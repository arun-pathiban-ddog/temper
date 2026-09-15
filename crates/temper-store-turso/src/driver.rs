//! SQL operations shared by the official embedded and serverless Turso drivers.

use thiserror::Error;

use turso_serverless::params::{IntoParams, Params};
pub(crate) use turso_serverless::{Row, Value, params, params_from_iter};

/// Driver failures retain the original engine or transport error.
#[derive(Debug, Error)]
pub(crate) enum DriverError {
    #[error(transparent)]
    Local(#[from] turso::Error),
    #[error(transparent)]
    Remote(turso_serverless::Error),
    #[error("remote Turso busy: {0}")]
    RemoteBusy(#[source] turso_serverless::Error),
    #[error("remote Turso transport failure: {0}")]
    RemoteTransport(#[source] turso_serverless::Error),
}

impl From<turso_serverless::Error> for DriverError {
    fn from(error: turso_serverless::Error) -> Self {
        match error {
            turso_serverless::Error::Busy(_) | turso_serverless::Error::BusySnapshot(_) => {
                Self::RemoteBusy(error)
            }
            turso_serverless::Error::Http(ref message)
                if message.starts_with("request to ")
                    || message.starts_with("cursor stream failed:") =>
            {
                Self::RemoteTransport(error)
            }
            _ => Self::Remote(error),
        }
    }
}

#[derive(Debug)]
pub(crate) enum Database {
    Local(turso::Database),
    Remote(turso_serverless::Database),
}

impl Database {
    pub(crate) async fn local(path: &str) -> Result<Self, DriverError> {
        Ok(Self::Local(turso::Builder::new_local(path).build().await?))
    }

    pub(crate) async fn remote(url: &str, token: &str) -> Result<Self, DriverError> {
        Ok(Self::Remote(
            turso_serverless::Builder::new_remote(url)
                .with_auth_token(token)
                .build()
                .await?,
        ))
    }

    pub(crate) fn connect(&self) -> Result<Connection, DriverError> {
        match self {
            Self::Local(db) => {
                let conn = db.connect()?;
                conn.busy_timeout(std::time::Duration::from_secs(5))?;
                Ok(Connection::Local(conn))
            }
            Self::Remote(db) => Ok(Connection::Remote(db.connect()?)),
        }
    }
}

pub(crate) enum Connection {
    Local(turso::Connection),
    Remote(turso_serverless::Connection),
}

impl Drop for Connection {
    fn drop(&mut self) {
        if let Self::Remote(conn) = self {
            // The SDK defers rollback to connection reuse. Store operations own
            // separate connections, so close the stream when their scope ends.
            let conn = conn.clone();
            match tokio::runtime::Handle::try_current() {
                Ok(runtime) => {
                    runtime.spawn(async move {
                        if let Err(error) = conn.close().await {
                            tracing::warn!(%error, "failed to close remote Turso connection");
                        }
                    });
                }
                Err(error) => {
                    tracing::warn!(%error, "cannot close remote Turso connection without a runtime");
                }
            }
        }
    }
}

impl Connection {
    pub(crate) async fn query(
        &self,
        sql: &str,
        params: impl IntoParams,
    ) -> Result<Rows, DriverError> {
        let params = params.into_params()?;
        match self {
            Self::Local(conn) => Rows::local(conn.query(sql, local_params(params)).await?).await,
            Self::Remote(conn) => Ok(Rows::Remote(conn.query(sql, params).await?)),
        }
    }

    pub(crate) async fn execute(
        &self,
        sql: &str,
        params: impl IntoParams,
    ) -> Result<u64, DriverError> {
        let params = params.into_params()?;
        match self {
            Self::Local(conn) => Ok(conn.execute(sql, local_params(params)).await?),
            Self::Remote(conn) => Ok(conn.execute(sql, params).await?),
        }
    }

    pub(crate) async fn begin_immediate(&self) -> Result<Transaction<'_>, DriverError> {
        // Each store operation owns its connection. The SDK still refuses a nested transaction.
        match self {
            Self::Local(conn) => Ok(Transaction::Local(
                turso::transaction::Transaction::new_unchecked(
                    conn,
                    turso::transaction::TransactionBehavior::Immediate,
                )
                .await?,
            )),
            Self::Remote(conn) => Ok(Transaction::Remote(
                turso_serverless::Transaction::new_unchecked(
                    conn,
                    turso_serverless::TransactionBehavior::Immediate,
                )
                .await?,
            )),
        }
    }
}

pub(crate) enum Transaction<'conn> {
    Local(turso::transaction::Transaction<'conn>),
    Remote(turso_serverless::Transaction<'conn>),
}

impl Transaction<'_> {
    pub(crate) async fn query(
        &self,
        sql: &str,
        params: impl IntoParams,
    ) -> Result<Rows, DriverError> {
        let params = params.into_params()?;
        match self {
            Self::Local(tx) => Rows::local(tx.query(sql, local_params(params)).await?).await,
            Self::Remote(tx) => Ok(Rows::Remote(tx.query(sql, params).await?)),
        }
    }

    pub(crate) async fn execute(
        &self,
        sql: &str,
        params: impl IntoParams,
    ) -> Result<u64, DriverError> {
        let params = params.into_params()?;
        match self {
            Self::Local(tx) => Ok(tx.execute(sql, local_params(params)).await?),
            Self::Remote(tx) => Ok(tx.execute(sql, params).await?),
        }
    }

    pub(crate) async fn rollback(self) -> Result<(), DriverError> {
        match self {
            Self::Local(tx) => Ok(tx.rollback().await?),
            Self::Remote(tx) => Ok(tx.rollback().await?),
        }
    }

    pub(crate) async fn commit(self) -> Result<(), DriverError> {
        match self {
            Self::Local(tx) => Ok(tx.commit().await?),
            Self::Remote(tx) => Ok(tx.commit().await?),
        }
    }
}

pub(crate) enum Rows {
    Local {
        rows: turso::Rows,
        first: Option<Row>,
    },
    Remote(turso_serverless::Rows),
    Exhausted,
}

impl Rows {
    async fn local(mut rows: turso::Rows) -> Result<Self, DriverError> {
        // Start the statement before returning, including PRAGMAs whose rows are discarded.
        match rows.next().await? {
            Some(row) => Ok(Self::Local {
                rows,
                first: Some(remote_row(row)?),
            }),
            None => Ok(Self::Exhausted),
        }
    }

    pub(crate) async fn next(&mut self) -> Result<Option<Row>, DriverError> {
        let row = match self {
            Self::Remote(rows) => rows.next().await?,
            Self::Local { rows, first } => match first.take() {
                Some(row) => Some(row),
                None => rows.next().await?.map(remote_row).transpose()?,
            },
            Self::Exhausted => None,
        };
        if row.is_none() {
            *self = Self::Exhausted;
        }
        Ok(row)
    }
}

fn remote_row(row: turso::Row) -> Result<Row, DriverError> {
    let values = (0..row.column_count())
        .map(|i| row.get_value(i).map(remote_value))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Row::new(values))
}

fn local_params(params: Params) -> turso::params::Params {
    match params {
        Params::None => turso::params::Params::None,
        Params::Positional(values) => {
            turso::params::Params::Positional(values.into_iter().map(local_value).collect())
        }
        Params::Named(values) => turso::params::Params::Named(
            values
                .into_iter()
                .map(|(key, value)| (key, local_value(value)))
                .collect(),
        ),
    }
}

fn local_value(value: Value) -> turso::Value {
    match value {
        Value::Null => turso::Value::Null,
        Value::Integer(value) => turso::Value::Integer(value),
        Value::Real(value) => turso::Value::Real(value),
        Value::Text(value) => turso::Value::Text(value),
        Value::Blob(value) => turso::Value::Blob(value),
    }
}

fn remote_value(value: turso::Value) -> Value {
    match value {
        turso::Value::Null => Value::Null,
        turso::Value::Integer(value) => Value::Integer(value),
        turso::Value::Real(value) => Value::Real(value),
        turso::Value::Text(value) => Value::Text(value),
        turso::Value::Blob(value) => Value::Blob(value),
    }
}

#[cfg(test)]
mod tests;
