use std::sync::{Arc, Barrier};

#[test]
fn concurrent_connection_drops_do_not_close_reused_handles() {
    const WORKERS: usize = 16;
    const CYCLES: usize = 10_000;
    let barrier = Arc::new(Barrier::new(WORKERS));
    let workers: Vec<_> = (0..WORKERS)
        .map(|worker| {
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                runtime.block_on(async {
                    let database = turso::Builder::new_local(":memory:").build().await.unwrap();
                    barrier.wait();
                    for cycle in 0..CYCLES {
                        let mut connection = database.connect().unwrap();
                        connection
                            .execute("CREATE TABLE IF NOT EXISTS proof (value INTEGER)", ())
                            .await
                            .unwrap();
                        let transaction = connection.transaction().await.unwrap();
                        transaction.execute("DELETE FROM proof", ()).await.unwrap();
                        let value = (worker * CYCLES + cycle) as i64;
                        transaction
                            .execute("INSERT INTO proof VALUES (?1)", [value])
                            .await
                            .unwrap();
                        transaction.commit().await.unwrap();
                        let mut rows = connection
                            .query("SELECT value FROM proof", ())
                            .await
                            .unwrap();
                        assert_eq!(
                            rows.next().await.unwrap().unwrap().get::<i64>(0).unwrap(),
                            value
                        );
                        drop(rows);
                        drop(connection);
                    }
                });
            })
        })
        .collect();
    for worker in workers {
        worker.join().unwrap();
    }
}
