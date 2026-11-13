# -*- coding: utf-8 -*-
import json

import appconfig


def test_normalize_segment_label():
    assert appconfig.normalize_segment_label("  A4区  ") == "A4区"
    assert appconfig.normalize_segment_label("# A4区") == "A4区"
    assert appconfig.normalize_segment_label(None) == ""
    assert len(appconfig.normalize_segment_label("x" * 100)) == appconfig.SEGMENT_LABEL_MAX


def test_parse_segment_entry_with_label():
    base, lab, err = appconfig.parse_segment_entry("172.16.119 A4区")
    assert err == ""
    assert base == "172.16.119"
    assert lab == "A4区"
    base, lab, err = appconfig.parse_segment_entry("172.016.005.x # 东厢")
    assert base == "172.16.5"
    assert lab == "东厢"
    base, lab, err = appconfig.parse_segment_entry("8.8.8 公网")
    assert base is None and err


def test_save_and_load_labels(tmp_path, monkeypatch):
    seg_file = tmp_path / "segments.json"
    monkeypatch.setattr(appconfig, "SEG_FILE", str(seg_file))
    appconfig.save_segments(
        ["172.16.119", "172.16.120"], 1, 254,
        labels={"172.16.119": "A4区", "172.16.999": "幽灵", "172.16.120": "  "},
    )
    data = json.loads(seg_file.read_text(encoding="utf-8"))
    assert data["segments"] == ["172.16.119", "172.16.120"]
    assert data["labels"] == {"172.16.119": "A4区"}
    assert appconfig.load_segment_labels(data["segments"]) == {"172.16.119": "A4区"}


def test_parse_batch_then_save_like_api(tmp_path, monkeypatch):
    """模拟 POST /api/segments 行内备注 → 落盘。"""
    seg_file = tmp_path / "segments.json"
    monkeypatch.setattr(appconfig, "SEG_FILE", str(seg_file))
    norm, labels, bad = [], {}, []
    for s in ["10.0.0 A仓", "10.0.1", "8.8.8 公网"]:
        base, lab, err = appconfig.parse_segment_entry(s)
        if err or not base:
            bad.append(err or s)
            continue
        if base not in norm:
            norm.append(base)
        if lab:
            labels[base] = lab
    assert bad == ["8.8.8 公网"]
    appconfig.save_segments(norm, 1, 50, labels=labels)
    assert appconfig.load_segment_labels(norm) == {"10.0.0": "A仓"}


def test_attach_segment_labels_for_containers(tmp_path, monkeypatch):
    import server

    seg_file = tmp_path / "segments.json"
    monkeypatch.setattr(appconfig, "SEG_FILE", str(seg_file))
    monkeypatch.setattr(server.appconfig, "SEG_FILE", str(seg_file))
    appconfig.save_segments(["10.9.1"], 1, 254, labels={"10.9.1": "水冷A4"})
    rows = server._attach_segment_labels([{"ip": "10.9.1.20", "online": True}])
    assert rows[0]["label"] == "水冷A4"
    assert rows[0]["segment"] == "10.9.1"
    assert rows[0]["ip"] == "10.9.1.20"
