from src.data.position_data import PositionDataManager


class _FakeClient:
    def __init__(self, positions):
        self._positions = list(positions)
        self.fetch_count = 0

    def get_all_positions(self):
        self.fetch_count += 1
        return list(self._positions)


def _pos(symbol, amt, side, entry=100.0, mark=101.0, lev=10, upnl=1.0):
    return {
        "symbol": symbol,
        "positionAmt": str(amt),
        "positionSide": side,
        "entryPrice": str(entry),
        "markPrice": str(mark),
        "leverage": str(lev),
        "unRealizedProfit": str(upnl),
        "liquidationPrice": "50",
    }


def test_get_current_position_prefers_position_side_over_amount_sign():
    client = _FakeClient(
        [
            # Hedge 模式下某些网关会出现 amount 正值但 positionSide=SHORT
            _pos("BTCUSDT", 1.0, "SHORT"),
        ]
    )
    mgr = PositionDataManager(client)

    pos = mgr.get_current_position("BTCUSDT")

    assert pos is not None
    assert pos["side"] == "SHORT"
    assert pos["amount"] == 1.0


def test_get_current_position_marks_hedge_conflict_and_picks_primary_leg():
    client = _FakeClient(
        [
            _pos("BTCUSDT", 0.2, "LONG", mark=100.0),
            _pos("BTCUSDT", -0.5, "SHORT", mark=100.0),
        ]
    )
    mgr = PositionDataManager(client)

    pos = mgr.get_current_position("BTCUSDT")

    assert pos is not None
    assert pos["side"] == "SHORT"
    assert pos["amount"] == 0.5
    assert pos["hedge_conflict"] is True
    assert len(pos["legs"]) == 2


def test_get_all_positions_groups_once_and_selects_primary_leg_per_symbol():
    client = _FakeClient(
        [
            _pos("BTCUSDT", 0.2, "LONG", mark=100.0),
            _pos("BTCUSDT", -0.5, "SHORT", mark=100.0),
            _pos("ETHUSDT", 1.2, "LONG", mark=200.0),
        ]
    )
    mgr = PositionDataManager(client)

    positions = mgr.get_all_positions()

    assert set(positions.keys()) == {"BTCUSDT", "ETHUSDT"}
    assert positions["BTCUSDT"]["side"] == "SHORT"
    assert positions["ETHUSDT"]["side"] == "LONG"
    assert client.fetch_count == 1
