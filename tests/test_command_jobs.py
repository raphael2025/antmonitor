import db


def test_command_job_recovery_never_replays_uncertain_targets(conn):
    job_id = "a" * 32
    db.create_command_job(
        conn, job_id, "operator", "reboot", {},
        [("10.1.0.1", "stock"), ("10.1.0.2", "stock")],
    )
    db.update_command_job_targets(conn, job_id, ["10.1.0.1"], "DISPATCHING")

    assert db.recover_command_jobs(conn) == 1
    job = db.get_command_job(conn, job_id)
    assert job["state"] == "INTERRUPTED"
    targets = {row["ip"]: row for row in job["targets"]}
    assert targets["10.1.0.1"]["state"] == "UNCERTAIN"
    assert targets["10.1.0.1"]["uncertain"] == 1
    assert targets["10.1.0.2"]["state"] == "CANCELLED"

    # Recovery is idempotent and does not schedule or re-send any command.
    assert db.recover_command_jobs(conn) == 0
    again = db.get_command_job(conn, job_id)
    assert {row["ip"]: row["state"] for row in again["targets"]} == {
        "10.1.0.1": "UNCERTAIN",
        "10.1.0.2": "CANCELLED",
    }


def test_command_job_persists_per_target_terminal_results(conn):
    job_id = "b" * 32
    db.create_command_job(conn, job_id, "operator", "reboot", {},
                          [("10.1.0.3", "stock"), ("10.1.0.4", "uniplus")])
    db.update_command_job_targets(conn, job_id, [
        {"ip": "10.1.0.3", "ok": True, "msg": "sent", "state": "SUCCEEDED"},
        {"ip": "10.1.0.4", "ok": False, "uncertain": True,
         "msg": "response lost", "state": "UNCERTAIN"},
    ], "FAILED")
    db.update_command_job(conn, job_id, "COMPLETED")

    job = db.get_command_job(conn, job_id)
    assert (job["total"], job["done"], job["success"], job["failed"]) == (2, 2, 1, 1)
    assert {row["ip"]: row["state"] for row in job["targets"]} == {
        "10.1.0.3": "SUCCEEDED",
        "10.1.0.4": "UNCERTAIN",
    }
