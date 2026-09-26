from app.engine.stops import check_stop_target


def test_no_params_never_triggers():
    assert check_stop_target(100.0, 105.0, 95.0, stop_loss_pct=None, take_profit_pct=None) is None


def test_stop_hit_when_low_breaches():
    hit = check_stop_target(100.0, 101.0, 96.0, stop_loss_pct=3.0, take_profit_pct=None)
    assert hit is not None
    assert hit.price == 97.0
    assert "損切り" in hit.reason


def test_stop_not_hit_when_low_above_line():
    assert check_stop_target(100.0, 101.0, 98.0, stop_loss_pct=3.0, take_profit_pct=None) is None


def test_target_hit_when_high_reaches():
    hit = check_stop_target(100.0, 105.0, 99.0, stop_loss_pct=None, take_profit_pct=4.0)
    assert hit is not None
    assert hit.price == 104.0
    assert "利確" in hit.reason


def test_both_hit_same_bar_prefers_stop():
    hit = check_stop_target(100.0, 110.0, 90.0, stop_loss_pct=3.0, take_profit_pct=3.0)
    assert hit is not None and "損切り" in hit.reason


def test_zero_or_negative_or_blank_disables():
    assert check_stop_target(100.0, 101.0, 50.0, stop_loss_pct=0, take_profit_pct=None) is None
    assert check_stop_target(100.0, 101.0, 50.0, stop_loss_pct=-3.0, take_profit_pct=None) is None
    assert check_stop_target(100.0, 101.0, 50.0, stop_loss_pct="", take_profit_pct=None) is None
    assert check_stop_target(100.0, 101.0, 50.0, stop_loss_pct=None, take_profit_pct=None) is None


def test_string_number_is_accepted():
    # フォームから来る値は文字列のこともある
    hit = check_stop_target(100.0, 101.0, 96.0, stop_loss_pct="3.0", take_profit_pct=None)
    assert hit is not None and hit.price == 97.0


def test_non_positive_avg_price_never_triggers():
    assert check_stop_target(0.0, 101.0, 50.0, stop_loss_pct=3.0, take_profit_pct=None) is None
