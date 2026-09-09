use super::*;

#[tokio::test]
async fn local_rows_preserve_positional_and_named_values() {
    let db = Database::local(":memory:").await.unwrap();
    let conn = db.connect().unwrap();
    let mut rows = conn
        .query(
            "SELECT ?1, ?2, ?3, ?4, ?5",
            params![i64::MAX, -1.25, "Δ state", vec![0_u8, 255, 17], Value::Null,],
        )
        .await
        .unwrap();
    let row = rows.next().await.unwrap().unwrap();
    assert_eq!(row.get::<i64>(0).unwrap(), i64::MAX);
    assert_eq!(row.get::<f64>(1).unwrap(), -1.25);
    assert_eq!(row.get::<String>(2).unwrap(), "Δ state");
    assert_eq!(row.get::<Vec<u8>>(3).unwrap(), vec![0, 255, 17]);
    assert!(matches!(row.get_value(4).unwrap(), Value::Null));
    assert!(rows.next().await.unwrap().is_none());
    let mut rows = conn
        .query("SELECT :value", [(":value", "named")])
        .await
        .unwrap();
    assert_eq!(
        rows.next()
            .await
            .unwrap()
            .unwrap()
            .get::<String>(0)
            .unwrap(),
        "named"
    );
}

#[tokio::test]
async fn failed_transaction_rolls_back_before_next_connection_use() {
    let db = Database::local(":memory:").await.unwrap();
    let conn = db.connect().unwrap();
    conn.execute("CREATE TABLE values_to_commit(id INTEGER PRIMARY KEY)", ())
        .await
        .unwrap();
    {
        let tx = conn.begin_immediate().await.unwrap();
        tx.execute("INSERT INTO values_to_commit VALUES (1)", ())
            .await
            .unwrap();
        assert!(
            tx.execute("INSERT INTO values_to_commit VALUES (1)", ())
                .await
                .is_err()
        );
    }
    let mut rows = conn
        .query("SELECT COUNT(*) FROM values_to_commit", ())
        .await
        .unwrap();
    assert_eq!(
        rows.next().await.unwrap().unwrap().get::<i64>(0).unwrap(),
        0
    );
    drop(rows);
    let tx = conn.begin_immediate().await.unwrap();
    tx.execute("INSERT INTO values_to_commit VALUES (2)", ())
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let mut rows = conn
        .query("SELECT id FROM values_to_commit", ())
        .await
        .unwrap();
    assert_eq!(
        rows.next().await.unwrap().unwrap().get::<i64>(0).unwrap(),
        2
    );
}

#[tokio::test]
async fn query_executes_configuration_even_when_rows_are_discarded() {
    let db = Database::local(":memory:").await.unwrap();
    let conn = db.connect().unwrap();
    drop(conn.query("PRAGMA user_version=17", ()).await.unwrap());
    let mut rows = conn.query("PRAGMA user_version", ()).await.unwrap();
    assert_eq!(
        rows.next().await.unwrap().unwrap().get::<i64>(0).unwrap(),
        17
    );
}
