"""Nightly reconciliation (plan §7.7, §15.3). Port of apps/gateway/src/reconcile.ts.

Ask each ADMS terminal to re-send yesterday's ATTLOG so the idempotent ingest can fill any gap; Hikvision terminals are
reconciled by AcsEvent search from the API side (Appendix C) once a device-management credential store exists.

    manage.py gateway_reconcile --all                       # yesterday, every active ADMS device
    manage.py gateway_reconcile --sn DEMO-01 --date 2026-09-21
Run from cron at 01:00 NPT or from the API worker.
"""
from __future__ import annotations


def reconcile_day(store, sn: str, date: str) -> dict:
    device = store.get_device(sn)
    if not device:
        return {'sn': sn, 'date': date, 'stored': 0, 'commandId': None, 'note': 'unknown device'}
    stored = store.count_punches(device.id, date)
    if device.status != 'active':
        return {'sn': sn, 'date': date, 'stored': stored, 'commandId': None, 'note': f'device {device.status}'}
    if device.protocol != 'adms':
        return {'sn': sn, 'date': date, 'stored': stored, 'commandId': None,
                'note': 'reconciliation for this protocol runs from the API'}
    command_id = store.enqueue_command(device.id, f'DATA QUERY ATTLOG StartTime={date} 00:00:00\tEndTime={date} 23:59:59')
    return {'sn': sn, 'date': date, 'stored': stored, 'commandId': command_id,
            'note': 'query queued; re-sent records are deduplicated on ingest'}
