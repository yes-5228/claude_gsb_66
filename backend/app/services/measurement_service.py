"""监测数据录入业务逻辑 (含超标自动判定)."""
from ..domain import exceedance_rules
from ..domain.standards import get_pollutant
from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..models import Exceedance, Measurement, Station


def get_measurement(measurement_id):
    measurement = db.session.get(Measurement, measurement_id)
    if measurement is None:
        raise NotFoundError("监测数据不存在: id=%s" % measurement_id)
    return measurement


def preview_entries(period, entries):
    """Dry-run evaluation for the entry form (no database writes)."""
    results = []
    for entry in entries:
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        if meta is None:
            raise ValidationError("未知监测因子: %s" % entry.get("pollutant"), fields={"pollutant": "unknown"})
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            raise ValidationError(
                "%s 监测值必须为数字" % meta["label"], fields={pollutant: "invalid_number"}
            )
        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        results.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                **evaluation,
            }
        )
    return {"period": period, "results": results, "summary": exceedance_rules.summarize(results)}


def _load_station(station_id):
    station = db.session.get(Station, station_id)
    if station is None:
        raise NotFoundError("监测点不存在: id=%s" % station_id)
    return station


def record_entries(station_id, measured_at, period, entries, data_source="manual",
                   recorder=None, remark=None, overwrite=False):
    """Persist one measured_at snapshot for a station.

    Duplicate (station, pollutant, period, measured_at) rows are reported back;
    when ``overwrite`` is true the existing row is refreshed instead.
    """
    station = _load_station(station_id)
    if not entries:
        raise ValidationError("至少需要录入一条监测数据", fields={"entries": "empty"})

    existing = {
        row.pollutant: row
        for row in Measurement.query.filter_by(
            station_id=station.id, period=period, measured_at=measured_at
        ).all()
    }

    created, updated, exceeded, duplicates, evaluated = [], [], [], [], []
    revoked, reinstated = [], []
    seen = set()
    for entry in entries:
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        if meta is None:
            raise ValidationError(
                "未知监测因子: %s" % entry.get("pollutant"), fields={"pollutant": "unknown"}
            )
        if pollutant in seen:
            raise ValidationError(
                "%s 在同一时刻重复提交" % meta["label"], fields={pollutant: "duplicated_in_batch"}
            )
        seen.add(pollutant)

        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            raise ValidationError(
                "%s 监测值必须为数字" % meta["label"], fields={pollutant: "invalid_number"}
            )

        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        evaluated.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                **evaluation,
            }
        )

        record = existing.get(pollutant)
        if record is not None and not overwrite:
            duplicates.append(
                {
                    "pollutant": pollutant,
                    "pollutant_label": meta["label"],
                    "value": value,
                    "existing_id": record.id,
                    "message": "该时刻 %s 数据已存在" % meta["label"],
                }
            )
            continue

        is_new = record is None
        if is_new:
            record = Measurement(station_id=station.id, pollutant=pollutant, period=period,
                                 measured_at=measured_at)
            db.session.add(record)

        record.value = value
        record.unit = meta["unit"]
        record.limit_value = evaluation["limit"]
        record.exceed_ratio = evaluation["ratio"]
        record.is_exceeded = evaluation["exceeded"]
        record.data_source = data_source
        record.recorder = entry.get("recorder") or recorder
        record.remark = entry.get("remark") or remark

        sync_event = _sync_exceedance(record, meta, evaluation, actor=record.recorder)
        db.session.flush()
        (created if is_new else updated).append(record.to_dict(include_station=True))
        if evaluation["exceeded"]:
            exceeded.append(record.exceedance.to_dict() if record.exceedance else None)
        if sync_event == "revoked":
            revoked.append(record.exceedance.to_dict())
        elif sync_event == "reinstated":
            reinstated.append(record.exceedance.to_dict())

    if not created and not updated and duplicates:
        raise ConflictError(
            "所选时刻已存在相同数据, 如需覆盖请勾选\"覆盖已有数据\": %s"
            % ", ".join(item["pollutant_label"] for item in duplicates)
        )

    db.session.commit()
    return {
        "station": station.to_option(),
        "measured_at": measured_at.isoformat(timespec="seconds"),
        "period": period,
        "created": created,
        "updated": updated,
        "exceedances": [item for item in exceeded if item],
        "revoked_exceedances": revoked,
        "reinstated_exceedances": reinstated,
        "duplicates": duplicates,
        "evaluations": evaluated,
        "summary": {
            "created_count": len(created),
            "updated_count": len(updated),
            "exceeded_count": len([item for item in evaluated if item["exceeded"]]),
            "duplicate_count": len(duplicates),
            "revoked_count": len(revoked),
            "reinstated_count": len(reinstated),
        },
    }


def _sync_exceedance(record, meta, evaluation, actor=None):
    """Reconcile the exceedance row with the latest judgement.

    数据修正只改变判定结果, 不删除处置痕迹: 不再超标时记录转为 ``revoked``
    (保留原标注人/说明/时间), 重新超标时回到 ``pending`` 等待重新复核,
    每次变化都追加一条 :class:`ExceedanceEvent` 并递增 ``revision``。
    返回发生的事件类型 (无变化时返回 None)。
    """
    exceedance = record.exceedance
    if evaluation["exceeded"]:
        if exceedance is None:
            exceedance = Exceedance(
                station_id=record.station_id,
                pollutant=record.pollutant,
                period=record.period,
                measured_at=record.measured_at,
                value=record.value,
                limit_value=evaluation["limit"],
                exceed_ratio=evaluation["ratio"],
                level=evaluation["level"],
                status="pending",
                revision=1,
            )
            record.exceedance = exceedance
            exceedance.log_event("created", actor=actor, note="数据录入判定超标, 自动建单")
            return "created"

        changed = (
            exceedance.value != record.value
            or exceedance.limit_value != evaluation["limit"]
            or exceedance.level != evaluation["level"]
        )
        was_revoked = exceedance.status == "revoked"
        exceedance.value = record.value
        exceedance.limit_value = evaluation["limit"]
        exceedance.exceed_ratio = evaluation["ratio"]
        exceedance.level = evaluation["level"]
        exceedance.measured_at = record.measured_at
        if was_revoked:
            # 修正后重新超标: 撤销作废, 但需重新复核, 历史标注保留在记录与事件流中
            exceedance.status = "pending"
            exceedance.revision += 1
            exceedance.log_event("reinstated", actor=actor,
                                 note="数据修正后重新判定超标, 记录重新进入待标注")
            return "reinstated"
        if changed:
            exceedance.revision += 1
            exceedance.log_event("corrected", actor=actor,
                                 note="数据修正后仍超标, 判定结果已更新")
            return "corrected"
        return None

    if exceedance is not None and exceedance.status != "revoked":
        # 修正后不再超标: 不删除记录, 标注痕迹随记录保留
        exceedance.status = "revoked"
        exceedance.revision += 1
        exceedance.log_event("revoked", actor=actor,
                             note="数据修正后不再超标, 记录撤销, 标注痕迹保留")
        return "revoked"
    return None


def delete_measurement(measurement):
    payload = measurement.to_dict()
    db.session.delete(measurement)
    db.session.commit()
    return payload
