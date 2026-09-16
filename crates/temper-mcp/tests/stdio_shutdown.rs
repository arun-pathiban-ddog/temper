//! Native pipes exercise Tokio's blocking standard-I/O threads, unlike duplex tests.
use std::io::Write;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

struct Process(Child);

impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn spawn() -> Process {
    let mut command = Command::new(env!("CARGO_BIN_EXE_temper-mcp"));
    for (name, _) in std::env::vars_os() {
        if name.to_string_lossy().starts_with("TEMPER_") {
            command.env_remove(name);
        }
    }
    Process(
        command
            .args(["--port", "1"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("start MCP executable"),
    )
}

fn assert_failed_exit(process: &mut Process) {
    // Allow process startup under a loaded workspace test runner as well as
    // the one-second runtime shutdown; the old executable hangs indefinitely.
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = process.0.try_wait().expect("read child status") {
            assert!(!status.success(), "transport failure must report an error");
            return;
        }
        assert!(Instant::now() < deadline, "MCP process failed to terminate");
        thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn broken_output_exits_while_input_remains_open() {
    let mut process = spawn();
    drop(process.0.stdout.take());
    let input = process.0.stdin.as_mut().expect("stdin pipe");
    writeln!(input, r#"{{"jsonrpc":"2.0","id":1,"method":"ping"}}"#).expect("write ping");
    input.flush().expect("flush ping");
    assert_failed_exit(&mut process);
}

#[test]
fn overloaded_input_exits_while_output_is_blocked() {
    let mut process = spawn();
    let mut input = process.0.stdin.take().expect("stdin pipe");
    let producer = thread::spawn(move || {
        // Fill the unread pipe and outbound queue, then overload input.
        for index in 0..265 {
            let id = if index < 65 {
                format!("{index}{}", "x".repeat(16_000))
            } else {
                index.to_string()
            };
            let request = serde_json::json!({"jsonrpc":"2.0", "id":id, "method":"ping"});
            if writeln!(input, "{request}")
                .and_then(|()| input.flush())
                .is_err()
            {
                return;
            }
            if index < 65 {
                thread::sleep(Duration::from_millis(10));
            }
        }
    });
    assert_failed_exit(&mut process);
    producer.join().expect("input producer");
}
