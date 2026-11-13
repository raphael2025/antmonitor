# -*- coding: utf-8 -*-
import time

import db


def test_powercut_is_quiet_but_stays_on_roster(tmp_path):
    conn = db.init_db(str(tmp_path / "t.db"))
    now = int(time.time())
    db.upsert_known_miners(conn, [{
        "ip": "10.0.0.1", "status": "online", "model": "S19", "sn": "A",
        "mac": "", "worker": "w", "firmware": "stock", "hr_rt": 100,
    }], now)
    db.set_machine_state(conn, ["10.0.0.1"], "powercut")
    assert "10.0.0.1" in db.powercut_ips(conn)
    assert "10.0.0.1" in db.quiet_ips(conn)
    assert "10.0.0.1" not in db.repair_ips(conn)
    assert "10.0.0.1" in db.roster_ips(conn, 7)
    db.set_machine_state(conn, ["10.0.0.1"], "active")
    assert "10.0.0.1" not in db.quiet_ips(conn)
