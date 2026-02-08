from src.trading.position_state_machine import (
    PositionStateMachineV2,
    PositionSnapshot,
    PositionLifecycle,
    ProtectionState,
)

from src.trading.intents import PositionSide

import sys
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DummyClient:
    def __init__(self):
        pass


def test_on_order_filled_with_none_qty_creates_zero_snapshot():
    sm = PositionStateMachineV2(client=DummyClient())

    # No pre-existing snapshot; filled_qty is None -> should create snapshot with qty 0.0
    sm.on_order_filled(symbol="AAA", position_side="LONG", order_id=None, filled_qty=None)

    assert "AAA" in sm.snapshots
    snap = sm.snapshots["AAA"]
    assert snap.side == PositionSide.LONG
    assert snap.quantity == 0.0
    assert snap.lifecycle == PositionLifecycle.OPEN


def test_on_order_canceled_with_none_is_noop_on_protection():
    sm = PositionStateMachineV2(client=DummyClient())

    # Create a protected snapshot
    snap = PositionSnapshot(symbol="BBB", side=PositionSide.SHORT, quantity=1.2, lifecycle=PositionLifecycle.PROTECTED)
    snap.protection = ProtectionState(take_profit=2.0, stop_loss=1.0, tp_order_id=111, sl_order_id=222)
    sm.snapshots["BBB"] = snap

    # Call with order_id None -> should return early and leave protection intact
    sm.on_order_canceled(symbol="BBB", order_id=None)
    snap_after = sm.snapshots["BBB"]
    assert snap_after.protection.tp_order_id == 111
    assert snap_after.protection.sl_order_id == 222
    assert snap_after.lifecycle == PositionLifecycle.PROTECTED


def test_on_position_update_with_none_is_noop():
    sm = PositionStateMachineV2(client=DummyClient())

    # Create a snapshot
    snap = PositionSnapshot(symbol="CCC", side=PositionSide.LONG, quantity=3.4, lifecycle=PositionLifecycle.OPEN)
    sm.snapshots["CCC"] = snap

    # Call with None -> should be no-op and snapshot remains
    sm.on_position_update(symbol="CCC", position_amt=None, position_side=None)
    assert "CCC" in sm.snapshots
    assert sm.snapshots["CCC"].quantity == 3.4


def test_on_order_filled_triggers_protection_removal_when_matching_order_id():
    sm = PositionStateMachineV2(client=DummyClient())

    # Create a snapshot with protection where tp_order_id == 333
    snap = PositionSnapshot(symbol="DDD", side=PositionSide.LONG, quantity=5.0, lifecycle=PositionLifecycle.PROTECTED)
    snap.protection = ProtectionState(take_profit=10.0, stop_loss=4.0, tp_order_id=333, sl_order_id=None)
    sm.snapshots["DDD"] = snap

    # Fill event with order_id == 333 should delete the snapshot
    sm.on_order_filled(symbol="DDD", position_side="LONG", order_id=333, filled_qty=0.0)
    assert "DDD" not in sm.snapshots


def test_on_position_update_zero_removes_snapshot():
    sm = PositionStateMachineV2(client=DummyClient())

    snap = PositionSnapshot(symbol="EEE", side=PositionSide.SHORT, quantity=2.0, lifecycle=PositionLifecycle.OPEN)
    sm.snapshots["EEE"] = snap

    sm.on_position_update(symbol="EEE", position_amt=0.0, position_side="SHORT")
    assert "EEE" not in sm.snapshots
