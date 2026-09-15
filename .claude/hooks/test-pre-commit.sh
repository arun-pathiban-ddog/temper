#!/bin/bash
# Exercise the actual staged-content hook in disposable repositories.
set -euo pipefail
hook_path="$(cd "$(dirname "$0")" && pwd)/pre-commit.sh"
task_tmp="$(mktemp -d "${TMPDIR:-/tmp}/temper-hook-test.XXXXXX")"
trap 'rm -rf "$task_tmp"' EXIT

check_case() {
    local name="$1" file="$2" source="$3" expected="$4"
    local repo="$task_tmp/$name" actual=0
    mkdir -p "$repo/$(dirname "$file")"
    git init -q "$repo"
    printf '%s\n' "$source" > "$repo/$file"
    git -C "$repo" add -- "$file"
    (cd "$repo" && bash "$hook_path") > "$repo/result.log" 2>&1 || actual=$?
    if [ "$actual" -ne "$expected" ]; then
        cat "$repo/result.log" >&2
        printf 'FAIL %s: expected %s, got %s\n' "$name" "$expected" "$actual" >&2
        exit 1
    fi
    printf 'PASS %s\n' "$name"
}

check_case removed_vendor_exception vendor/libsql/src/lib.rs 'fn upstream() { todo!(); }' 1
check_case adjacent vendor/libsql-extra/src/lib.rs 'fn unexpected() { todo!(); }' 1
check_case first_party crates/example/src/lib.rs 'fn incomplete() { todo!(); }' 1
check_case complete crates/example/src/lib.rs 'fn complete() -> bool { true }' 0
