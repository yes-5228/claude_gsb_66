"""数据修正与超标记录处置痕迹分离的回归测试.

覆盖场景: 修正/覆盖已超标数据时, 标注结论不丢失、状态变化有留痕、
已处理事项不被静默作废、结论变化可归属到具体修正版本.
"""
from app.models import Exceedance, ExceedanceEvent, Measurement


def _enter(client, station, entry_payload, entries, measured_at="2026-09-01 10:00", **kw):
    return client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, measured_at=measured_at, entries=entries, **kw),
    )


def _only_exceedance():
    return Exceedance.query.one()


def test_correction_keeps_annotation_and_marks_stale(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    exceedance = _only_exceedance()
    client.patch(
        "/api/exceedances/%d" % exceedance.id,
        json={"status": "confirmed", "note": "已通知现场核查", "annotator": "王敏"},
    )

    # 修正后仍超标(等级变化): 记录不重建, 标注保留但标记待复核
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 1200.0}], overwrite=True)

    assert Exceedance.query.count() == 1
    exceedance = _only_exceedance()
    assert exceedance.status == "confirmed"
    assert exceedance.annotator == "王敏"
    assert exceedance.note == "已通知现场核查"
    assert exceedance.annotated_at is not None
    assert exceedance.annotation_stale is True
    assert exceedance.level == "severe"

    corrected = exceedance.events[-1]
    assert corrected.event_type == "corrected"
    assert corrected.measurement_revision == 2
    assert corrected.prev_value == 600.0
    assert corrected.value == 1200.0
    assert corrected.prev_level == "light"
    assert corrected.level == "severe"
    assert corrected.prev_status == "confirmed"
    assert corrected.status == "confirmed"


def test_correction_below_limit_revokes_but_keeps_trail(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    exceedance = _only_exceedance()
    client.patch(
        "/api/exceedances/%d" % exceedance.id,
        json={"status": "ignored", "note": "校准期间异常值", "annotator": "李静"},
    )
    summary_before = client.get("/api/exceedances/summary").get_json()
    assert summary_before["by_status"][2]["count"] == 1  # ignored

    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 100.0}], overwrite=True)

    # 已处理事项不被作废: 记录仍在, 标注痕迹完整, 状态变为已撤销
    assert Exceedance.query.count() == 1
    exceedance = _only_exceedance()
    assert exceedance.status == "revoked"
    assert exceedance.annotator == "李静"
    assert exceedance.note == "校准期间异常值"
    assert exceedance.annotated_at is not None

    revoked = exceedance.events[-1]
    assert revoked.event_type == "revoked"
    assert revoked.measurement_revision == 2
    assert revoked.prev_status == "ignored"
    assert revoked.status == "revoked"
    assert revoked.prev_value == 600.0
    assert revoked.value == 100.0

    summary_after = client.get("/api/exceedances/summary").get_json()
    by_status = {item["key"]: item["count"] for item in summary_after["by_status"]}
    assert by_status["revoked"] == 1
    assert by_status["ignored"] == 0
    assert summary_after["total"] == 1  # 记录不消失


def test_revoked_record_restored_to_pending_on_new_exceedance(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    exceedance = _only_exceedance()
    client.patch(
        "/api/exceedances/%d" % exceedance.id,
        json={"status": "confirmed", "note": "复核属实", "annotator": "王敏"},
    )
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 100.0}], overwrite=True)
    assert _only_exceedance().status == "revoked"

    # 再次修正回超标: 同一条记录恢复待办, 历史标注仍可查, 标记待复核
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 800.0}], overwrite=True)

    assert Exceedance.query.count() == 1
    exceedance = _only_exceedance()
    assert exceedance.status == "pending"
    assert exceedance.annotation_stale is True
    assert exceedance.annotator == "王敏"  # 此前由谁标注过依然可见
    assert exceedance.note == "复核属实"
    assert exceedance.value == 800.0

    event_types = [event.event_type for event in exceedance.events]
    assert event_types == ["created", "annotated", "revoked", "restored"]
    restored = exceedance.events[-1]
    assert restored.measurement_revision == 3
    assert restored.prev_status == "revoked"
    assert restored.status == "pending"


def test_revoked_record_cannot_be_annotated(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 100.0}], overwrite=True)
    exceedance = _only_exceedance()
    assert exceedance.status == "revoked"

    response = client.patch(
        "/api/exceedances/%d" % exceedance.id,
        json={"status": "confirmed", "note": "试图标注已撤销记录"},
    )
    assert response.status_code == 422
    assert exceedance.status == "revoked"

    # 人工也不能把记录直接置为系统状态 revoked
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 900.0}], overwrite=True)
    response = client.patch(
        "/api/exceedances/%d" % exceedance.id, json={"status": "revoked", "note": "x"}
    )
    assert response.status_code == 422


def test_batch_annotation_skips_revoked_records(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    _enter(
        client, station, entry_payload,
        [{"pollutant": "NO2", "value": 300.0}], measured_at="2026-09-01 11:00",
    )
    # 把 SO2 修正到限值以下 -> 撤销
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 100.0}], overwrite=True)

    ids = [item.id for item in Exceedance.query.all()]
    response = client.post(
        "/api/exceedances/annotations",
        json={"ids": ids, "status": "confirmed", "note": "批量复核", "annotator": "王敏"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["updated"] == 1
    assert len(body["skipped"]) == 1

    statuses = {item.pollutant: item.status for item in Exceedance.query.all()}
    assert statuses == {"SO2": "revoked", "NO2": "confirmed"}


def test_reannotation_clears_stale_flag_and_logs_event(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    exceedance = _only_exceedance()
    client.patch(
        "/api/exceedances/%d" % exceedance.id,
        json={"status": "confirmed", "note": "首次确认", "annotator": "王敏"},
    )
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 700.0}], overwrite=True)
    assert _only_exceedance().annotation_stale is True

    client.patch(
        "/api/exceedances/%d" % exceedance.id,
        json={"status": "confirmed", "note": "修正后复核仍属实", "annotator": "李静"},
    )
    exceedance = _only_exceedance()
    assert exceedance.annotation_stale is False
    assert exceedance.annotator == "李静"
    annotated = exceedance.events[-1]
    assert annotated.event_type == "annotated"
    assert annotated.actor == "李静"
    assert annotated.measurement_revision == 2


def test_detail_response_includes_event_trail(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    exceedance = _only_exceedance()
    client.patch(
        "/api/exceedances/%d" % exceedance.id,
        json={"status": "confirmed", "note": "复核属实", "annotator": "王敏"},
    )
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 100.0}], overwrite=True)

    detail = client.get("/api/exceedances/%d" % exceedance.id).get_json()
    assert detail["status"] == "revoked"
    assert detail["annotator"] == "王敏"
    assert detail["measurement_revision"] == 2
    trail = [(event["event_type"], event["event_type_label"]) for event in detail["events"]]
    assert trail == [
        ("created", "自动建单"),
        ("annotated", "人工标注"),
        ("revoked", "判定撤销"),
    ]
    revoked_event = detail["events"][-1]
    assert revoked_event["actor"] == "测试员"  # 修正操作的录入人
    assert revoked_event["measurement_revision"] == 2
    assert revoked_event["prev_status_label"] == "已确认"


def test_summary_reports_revoked_and_stale(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    _enter(
        client, station, entry_payload,
        [{"pollutant": "NO2", "value": 300.0}], measured_at="2026-09-01 11:00",
    )
    exceedances = Exceedance.query.order_by(Exceedance.id.asc()).all()
    client.patch(
        "/api/exceedances/%d" % exceedances[0].id,
        json={"status": "confirmed", "note": "复核属实", "annotator": "王敏"},
    )
    # SO2 修正后仍超标 -> 待复核; NO2 保持待标注
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 700.0}], overwrite=True)

    summary = client.get("/api/exceedances/summary").get_json()
    assert summary["stale"] == 1
    assert summary["revoked"] == 0
    assert summary["pending"] == 1

    stale_only = client.get("/api/exceedances?stale=true").get_json()
    assert stale_only["total"] == 1
    assert stale_only["items"][0]["annotation_stale"] is True


def test_measurement_revision_increments_on_each_overwrite(client, station, entry_payload):
    _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": 600.0}])
    for value in (650.0, 700.0):
        _enter(client, station, entry_payload, [{"pollutant": "SO2", "value": value}],
               overwrite=True)

    measurement = Measurement.query.filter_by(pollutant="SO2").one()
    assert measurement.revision == 3
    body = client.get("/api/measurements?pollutant=SO2").get_json()
    assert body["items"][0]["revision"] == 3

    # 每次修正都留有事件, 且能对应到修正版本
    exceedance = _only_exceedance()
    revisions = [event.measurement_revision for event in exceedance.events]
    assert revisions == [1, 2, 3]
    assert ExceedanceEvent.query.count() == 3
