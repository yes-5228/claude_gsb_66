"""监测数据录入接口测试."""
from app.extensions import db
from app.models import Exceedance, Measurement


def test_batch_entry_creates_records_and_flags_exceedance(client, station, entry_payload):
    response = client.post("/api/measurements/entries", json=entry_payload(station.id))
    assert response.status_code == 201
    body = response.get_json()
    assert body["summary"]["created_count"] == 3
    assert body["summary"]["exceeded_count"] == 1
    assert len(body["exceedances"]) == 1
    assert body["exceedances"][0]["pollutant"] == "SO2"
    assert body["exceedances"][0]["status"] == "pending"
    assert body["station"]["code"] == "TEST-001"

    stored = Measurement.query.filter_by(pollutant="SO2").one()
    assert stored.is_exceeded is True
    assert stored.limit_value == 500.0
    assert stored.exceed_ratio == 1.8
    assert stored.unit == "μg/m³"
    assert stored.recorder == "测试员"


def test_duplicate_entry_is_reported_as_conflict(client, station, entry_payload):
    payload = entry_payload(station.id)
    client.post("/api/measurements/entries", json=payload)
    response = client.post("/api/measurements/entries", json=payload)
    assert response.status_code == 409
    assert "覆盖已有数据" in response.get_json()["error"]["message"]
    assert Measurement.query.count() == 3


def test_overwrite_below_limit_revokes_exceedance_but_keeps_trace(client, station, entry_payload):
    created = client.post("/api/measurements/entries", json=entry_payload(station.id)).get_json()
    exceedance_id = created["exceedances"][0]["id"]
    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "复核确认超标", "annotator": "王敏"},
    )

    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            overwrite=True,
            entries=[{"pollutant": "SO2", "value": 120.0}],
        ),
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["summary"]["created_count"] == 0
    assert body["summary"]["updated_count"] == 1
    assert body["summary"]["exceeded_count"] == 0
    assert body["summary"]["revoked_count"] == 1
    assert body["revoked_exceedances"][0]["id"] == exceedance_id
    assert Measurement.query.filter_by(pollutant="SO2").one().is_exceeded is False

    # 记录不删除: 状态转为已撤销, 标注痕迹保留
    record = db.session.get(Exceedance, exceedance_id)
    assert Exceedance.query.count() == 1
    assert record.status == "revoked"
    assert record.revision == 2
    assert record.annotator == "王敏"
    assert record.note == "复核确认超标"
    assert record.annotated_at is not None

    detail = client.get("/api/exceedances/%d" % exceedance_id).get_json()
    events = [(item["event"], item["status"]) for item in detail["events"]]
    assert events == [("created", "pending"), ("annotated", "confirmed"), ("revoked", "revoked")]
    # 撤销事件能定位到是哪一次修正引起的 (操作人 = 本次修正的录入人)
    assert detail["events"][-1]["actor"] == "测试员"
    assert detail["events"][-1]["revision"] == 2


def test_overwrite_back_above_limit_reinstates_record(client, station, entry_payload):
    created = client.post("/api/measurements/entries", json=entry_payload(station.id)).get_json()
    exceedance_id = created["exceedances"][0]["id"]
    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "复核确认超标", "annotator": "王敏"},
    )
    client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, overwrite=True, entries=[{"pollutant": "SO2", "value": 120.0}]),
    )
    assert db.session.get(Exceedance, exceedance_id).status == "revoked"

    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, overwrite=True, entries=[{"pollutant": "SO2", "value": 800.0}]),
    )
    body = response.get_json()
    assert body["summary"]["reinstated_count"] == 1
    assert body["reinstated_exceedances"][0]["id"] == exceedance_id

    # 重新超标后回到待标注, 但此前的标注人与说明仍然可查
    record = db.session.get(Exceedance, exceedance_id)
    assert record.status == "pending"
    assert record.revision == 3
    assert record.annotator == "王敏"
    assert record.note == "复核确认超标"

    detail = client.get("/api/exceedances/%d" % exceedance_id).get_json()
    assert [item["event"] for item in detail["events"]] == [
        "created", "annotated", "revoked", "reinstated",
    ]


def test_correction_while_still_exceeded_keeps_annotation_and_logs_event(
    client, station, entry_payload
):
    created = client.post("/api/measurements/entries", json=entry_payload(station.id)).get_json()
    exceedance_id = created["exceedances"][0]["id"]
    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "复核确认超标", "annotator": "王敏"},
    )

    client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, overwrite=True, entries=[{"pollutant": "SO2", "value": 650.0}]),
    )
    record = db.session.get(Exceedance, exceedance_id)
    assert record.status == "confirmed"  # 仍超标, 人工结论不被修正覆盖
    assert record.value == 650.0
    assert record.level == "light"
    assert record.revision == 2

    detail = client.get("/api/exceedances/%d" % exceedance_id).get_json()
    assert [item["event"] for item in detail["events"]] == ["created", "annotated", "corrected"]
    assert detail["events"][-1]["value"] == 650.0


def test_preview_validates_without_writing(client, station, entry_payload):
    payload = entry_payload(
        station.id,
        period="daily",
        entries=[{"pollutant": "PM25", "value": 90.0}, {"pollutant": "O3", "value": 100.0}],
    )
    payload.pop("station_id")
    response = client.post("/api/measurements/preview", json=payload)
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"] == {"total": 2, "exceeded_count": 1, "exceeded_pollutants": ["PM25"]}
    assert body["results"][0]["limit"] == 75.0
    assert body["results"][0]["level"] == "light"
    assert Measurement.query.count() == 0


def test_invalid_entries_are_rejected(client, station, entry_payload):
    unknown = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "XX", "value": 1}]),
    )
    assert unknown.status_code == 422

    non_numeric = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "PM25", "value": "abc"}]),
    )
    assert non_numeric.status_code == 422

    empty = client.post("/api/measurements/entries", json=entry_payload(station.id, entries=[]))
    assert empty.status_code == 422

    bad_station = client.post(
        "/api/measurements/entries", json=entry_payload(9999, entries=[{"pollutant": "PM25", "value": 10}])
    )
    assert bad_station.status_code == 404


def test_hourly_particulate_is_stored_without_limit(client, station, entry_payload):
    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(station.id, entries=[{"pollutant": "PM10", "value": 300.0}]),
    )
    assert response.status_code == 201
    record = Measurement.query.filter_by(pollutant="PM10").one()
    assert record.limit_value is None
    assert record.is_exceeded is False
    assert Exceedance.query.count() == 0


def test_list_measurements_with_filters(client, station, entry_payload):
    client.post("/api/measurements/entries", json=entry_payload(station.id))
    body = client.get("/api/measurements?station_id=%d&pollutant=SO2" % station.id).get_json()
    assert body["total"] == 1
    assert body["items"][0]["pollutant_label"] == "SO₂"
    assert body["items"][0]["station"]["code"] == "TEST-001"
    assert body["summary"]["exceeded_count"] == 1

    exceeded = client.get("/api/measurements?is_exceeded=true").get_json()
    assert exceeded["total"] == 1


def test_delete_measurement_removes_exceedance(client, station, entry_payload):
    created = client.post("/api/measurements/entries", json=entry_payload(station.id)).get_json()
    exceeded_id = created["exceedances"][0]["measurement_id"]
    response = client.delete("/api/measurements/%d" % exceeded_id)
    assert response.status_code == 200
    assert Exceedance.query.count() == 0
    assert Measurement.query.count() == 2


def test_entry_context_exposes_form_options(client, station):
    body = client.get("/api/measurements/entry-context").get_json()
    assert body["stations"][0]["code"] == "TEST-001"
    assert {item["value"] for item in body["periods"]} == {"hourly", "daily"}
    assert {item["value"] for item in body["data_sources"]} >= {"manual", "device"}


def test_export_measurements_csv(client, station, entry_payload):
    client.post("/api/measurements/entries", json=entry_payload(station.id))
    response = client.get("/api/measurements/export?station_id=%d" % station.id)
    assert response.status_code == 200
    assert "text/csv" in response.headers["Content-Type"]
    text = response.get_data(as_text=True)
    assert text.startswith("\ufeff站点编码")
    assert "测试监测点" in text
    assert len([line for line in text.strip().splitlines()]) == 4
