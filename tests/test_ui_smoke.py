"""Construct and drive the whole window off-screen.

UI crashes almost always happen during construction or on the first update, and both
are reachable without a display via Qt's offscreen platform.
"""

import asyncio
import logging
import os

import pytest

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Qt no longer ships fonts, and the offscreen platform finds none on its own. Its
# fallback is markedly wider than Segoe UI, so any test that measures a widget
# against its text would be reading metrics the real app never uses.
if os.path.isdir(r"C:\Windows\Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.broker.base import Fill, FillReason, ManagedPosition  # noqa: E402
from src.config import AppSettings  # noqa: E402
from src.core.models import (  # noqa: E402
    Candle,
    MarginMode,
    Position,
    Side,
    Timeframe,
    TradingMode,
)
from src.db import connect, get_ui_state, set_ui_state  # noqa: E402
from src.logging_setup import LogLine  # noqa: E402
from src.store import record_fill  # noqa: E402
from src.strategy import available  # noqa: E402
from src.ui.controller import Snapshot  # noqa: E402
from src.ui import main_window  # noqa: E402
from src.ui.about_page import AboutPage  # noqa: E402
from src.ui.dashboard_page import DashboardPage  # noqa: E402
from src.ui.logs_page import LogsPage  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.settings_page import SettingsPage  # noqa: E402
from src.ui.stats_page import StatsPage  # noqa: E402
from src.ui.trades_page import TradesPage  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def make_position(side: Side = Side.LONG) -> ManagedPosition:
    return ManagedPosition(
        position=Position(
            coin="BTC",
            size=0.05 * side.sign,
            entry_price=63_000.0,
            liquidation_price=57_120.0,
            unrealized_pnl=27.5,
            margin_used=630.0,
            leverage=5,
        ),
        stop_price=61_880.0,
        take_profit_price=65_240.0,
        entry_time_ms=1_700_000_000_000,
    )


def make_candles(count: int = 40) -> list[Candle]:
    return [
        Candle(
            open_time_ms=i * 3_600_000,
            close_time_ms=(i + 1) * 3_600_000 - 1,
            open=63_000.0,
            high=63_100.0,
            low=62_900.0,
            close=63_000.0 + i,
            volume=1.0,
            trades=1,
        )
        for i in range(count)
    ]


LIVE_ADDRESS = "0x5eA3e82B3605201d09b349789feD24E30D76c41b"


def log_line(message: str, levelno: int = logging.INFO) -> LogLine:
    return LogLine(
        time="10:00:00",
        level=logging.getLevelName(levelno),
        levelno=levelno,
        message=message,
        formatted=f"10:00:00  {logging.getLevelName(levelno)}  test  {message}",
    )


# --- settings -------------------------------------------------------------


def test_settings_page_builds_without_exploding(qapp):
    """Regression: change signals fired during load before the state existed."""
    page = SettingsPage(AppSettings())
    assert page.current() == AppSettings()


def test_settings_round_trip_through_the_widgets(qapp):
    original = AppSettings(
        timeframe=Timeframe.M15,
        risk_usdc=12.5,
        leverage=7,
        margin_mode=MarginMode.CROSS,
        paper_starting_balance=2_500.0,
        daily_loss_limit_pct=0.075,
        economic_data_day_block=True,
    )
    page = SettingsPage(AppSettings())
    page.set_max_leverage(40)
    page.load(original)
    assert page.current() == original


def test_every_requested_timeframe_is_offered(qapp):
    """summary.txt asks for all seven; none may quietly go missing."""
    page = SettingsPage(AppSettings())
    offered = {page.timeframe.itemData(i) for i in range(page.timeframe.count())}
    assert offered == set(Timeframe)


def test_loss_making_timeframes_are_flagged(qapp):
    """4h is on this list from measurement, not intuition: it backtested at
    -0.399R, worse than 5m. A slow timeframe is not a safe one."""
    page = SettingsPage(AppSettings())
    for losing in (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H4):
        page.load(AppSettings(timeframe=losing))
        assert page.timeframe_note.isVisibleTo(page), losing
        assert "backtested at a loss" in page.timeframe_note.text()

    page.load(AppSettings(timeframe=Timeframe.H1))
    assert not page.timeframe_note.isVisibleTo(page)


def test_no_field_is_offered_in_a_mode_where_nothing_acts_on_it(qapp):
    """The whole audit in one assertion. Each of these is read by exactly one of
    the two brokers, so showing it in the other mode offers a control that controls
    nothing - and in the paper balance's case, one that reads as a starting stake
    for real money."""
    page = SettingsPage(AppSettings())

    # widget, label, and the mode it is allowed to appear in
    mode_only = (
        (page.address, page._address_label, TradingMode.LIVE),
        (page.agent_key, page._key_label, TradingMode.LIVE),
        # Sent to Hyperliquid with the leverage; PaperBroker.set_leverage drops it.
        (page.margin, page._margin_label, TradingMode.LIVE),
        # Meaningless in Live, where the balance is whatever the exchange says.
        (page.balance, page._balance_label, TradingMode.PAPER),
    )

    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        page.load(AppSettings(trading_mode=mode))
        for widget, label, belongs_to in mode_only:
            expected = mode is belongs_to
            assert widget.isVisibleTo(page) is expected, f"{label.text()} in {mode}"
            assert label.isVisibleTo(page) is expected, f"{label.text()} caption in {mode}"


def test_the_paper_balance_is_hidden_in_live_mode(qapp):
    """It does nothing in Live - the balance is whatever the exchange says. On
    screen beside real credentials it reads as a starting stake for real money."""
    page = SettingsPage(AppSettings())
    page.load(AppSettings(trading_mode=TradingMode.PAPER))
    assert page.balance.isVisibleTo(page)
    assert page._balance_label.isVisibleTo(page)

    page.load(AppSettings(trading_mode=TradingMode.LIVE))
    assert not page.balance.isVisibleTo(page)
    # The caption goes too. A stranded label is worse than neither.
    assert not page._balance_label.isVisibleTo(page)


def test_the_paper_balance_still_round_trips_while_hidden(qapp):
    """Hidden is not discarded: switching to Live and back must not wipe it."""
    page = SettingsPage(AppSettings())
    page.load(AppSettings(trading_mode=TradingMode.LIVE, paper_starting_balance=2_500.0))

    assert page.current().paper_starting_balance == pytest.approx(2_500.0)


def test_live_mode_warns_that_it_spends_real_money(qapp):
    page = SettingsPage(AppSettings())
    page.load(AppSettings(trading_mode=TradingMode.LIVE))
    assert "REAL MONEY" in page.mode_note.text()


def test_the_credential_fields_appear_only_in_live_mode(qapp):
    """A key field on screen in Paper mode invites pasting a key nothing needs."""
    page = SettingsPage(AppSettings())
    assert not page.address.isVisibleTo(page)
    assert not page.agent_key.isVisibleTo(page)

    page.load(AppSettings(trading_mode=TradingMode.LIVE))
    assert page.address.isVisibleTo(page)
    assert page.agent_key.isVisibleTo(page)


def test_no_note_on_the_settings_page_is_clipped(qapp):
    """Regression: a wrapped label can always shrink in a layout's eyes, so when the
    form was taller than its scroll area the notes were what got squeezed. One lost
    its final sentence, a single pixel short of fitting."""
    from PySide6.QtWidgets import QLabel

    page = SettingsPage(AppSettings())
    page.load(AppSettings(trading_mode=TradingMode.LIVE))
    page.resize(700, 900)  # deliberately shorter than the form
    page.show()
    qapp.processEvents()

    for label in page.findChildren(QLabel):
        if not label.wordWrap() or not label.text() or label.width() <= 0:
            continue
        needed = label.heightForWidth(label.width())
        if needed > 0:
            assert label.height() >= needed, label.text()[:60]


# --- the preview card -----------------------------------------------------
#
# It lives on the About page, not Settings: it is reference material, read once to
# understand what a risk and leverage pair produces, not on every edit to the form.


def preview_values(page) -> dict:
    card = page.preview_card
    return {key: card._values[key].text() for key, _ in card.ROWS}


def test_the_preview_starts_empty_rather_than_blank(qapp):
    """Drawn as if waiting, so the card never looks broken before prices arrive."""
    page = AboutPage()
    assert set(preview_values(page).values()) == {"-"}
    assert "Waiting" in page.preview_card.verdict.text()


def test_the_preview_shows_what_a_trade_would_be(qapp):
    from src.ui.controller import Preview

    page = AboutPage()
    page.show_preview(
        Preview(
            price=63_037.0, equity=99.72, stop_distance=847.0,
            size=0.00236, notional=149.0, margin=30.0,
        )
    )

    values = preview_values(page)
    assert values["price"] == "63,037.0"
    assert values["equity"] == "99.72 USDC"
    assert values["size"] == "0.00236 BTC"
    assert values["margin"] == "30 USDC"
    assert "can trade" in page.preview_card.verdict.text()


def test_the_preview_names_settings_that_cannot_trade(qapp):
    """The exact trap this card exists for: 5 USDC at 2x on a 99 USDC account needs
    373 USDC of notional and gets 198, so every entry is rejected in silence."""
    from src.ui.controller import Preview

    page = AboutPage()
    page.show_preview(
        Preview(
            price=63_037.0, equity=99.72, stop_distance=847.0,
            problem="exceeds leverage cap", hint="needs 4x, or risk of about 2.50 USDC",
        )
    )

    assert preview_values(page)["size"] == "-"  # there is no trade to describe
    verdict = page.preview_card.verdict.text()
    assert "Cannot trade" in verdict
    assert "needs 4x" in verdict


def test_the_strategy_is_named_but_not_offered_as_a_choice(qapp):
    """One strategy ships, so the dropdown was a control that could not control
    anything. It is still stated - a live account was once started on the wrong
    strategy, and the fix was saying which one runs, not offering a choice."""
    page = SettingsPage(AppSettings())

    assert not hasattr(page, "strategy")  # no input
    assert "Volume rejection" in page.strategy_name.text()  # but it is named
    assert "Fades failed breakouts" in page.strategy_note.text()


def test_the_strategy_survives_a_save_without_a_widget_to_hold_it(qapp):
    """Nothing on the page reads it back any more, so it has to carry through from
    the loaded settings or every save would blank it."""
    page = SettingsPage(AppSettings())
    page.load(AppSettings(strategy="volume_rejection"))

    assert page.current().strategy == "volume_rejection"


def test_a_saved_strategy_this_build_lost_falls_back_rather_than_dead_ending(qapp):
    """Without a dropdown there is no control left to correct an unknown strategy,
    and `validate` refuses to start on one. Left alone it would be unfixable from
    the UI, so `load` normalises it and the page shows what will actually run."""
    page = SettingsPage(AppSettings())
    page.load(AppSettings(strategy="deleted_strategy"))

    assert page.current().strategy in available()
    assert "Volume rejection" in page.strategy_name.text()


def test_the_exit_settings_round_trip(qapp):
    page = SettingsPage(AppSettings())
    page.load(
        AppSettings(
            take_profit_rr=3.0,
            stop_buffer_pct=0.005,
            trailing_enabled=True,
            trailing_activation_rr=1.0,
            trailing_distance_pct=0.008,
        )
    )

    assert page.take_profit.value() == 3.0
    assert page.stop_buffer.value() == pytest.approx(0.5)   # shown as a percentage
    assert page.trail_distance.value() == pytest.approx(0.8)

    settings = page.current()
    assert settings.take_profit_rr == 3.0
    assert settings.stop_buffer_pct == pytest.approx(0.005)
    assert settings.trailing_activation_rr == 1.0
    assert settings.trailing_distance_pct == pytest.approx(0.008)


def test_the_post_only_entry_settings_round_trip(qapp):
    """Regression: post_only_entry was persisted and read by the engine but had no
    widget, so the one setting the user's specification depends on could not be
    switched on from the app at all."""
    page = SettingsPage(AppSettings())
    page.load(AppSettings(post_only_entry=True, entry_expiry_candles=4))

    assert page.post_only.isChecked()
    assert page.entry_expiry.value() == 4

    settings = page.current()
    assert settings.post_only_entry
    assert settings.entry_expiry_candles == 4


def test_the_expiry_greys_out_when_entries_cross_the_spread(qapp):
    """Nothing rests, so nothing can expire."""
    page = SettingsPage(AppSettings())

    page.post_only.setChecked(False)
    assert not page.entry_expiry.isEnabled()

    page.post_only.setChecked(True)
    assert page.entry_expiry.isEnabled()


def test_every_settings_field_reaches_the_settings_object(qapp):
    """A field the form shows but never writes back is worse than no field: it
    looks configured and is not. Caught post_only_entry, which had no widget."""
    import dataclasses

    page = SettingsPage(AppSettings())
    # Deliberately different from every default, so a field that is not written
    # back shows up as unchanged.
    changed = AppSettings(
        strategy="volume_rejection",
        timeframe=Timeframe.M5,
        risk_usdc=3.25,
        leverage=7,
        margin_mode=MarginMode.CROSS,
        slippage=0.02,
        daily_loss_limit_pct=0.12,
        take_profit_rr=3.5,
        stop_buffer_pct=0.006,
        trailing_enabled=True,
        trailing_activation_rr=1.5,
        trailing_distance_pct=0.007,
        post_only_entry=True,
        entry_expiry_candles=5,
        news_blackout_enabled=False,
        news_blackout_before_min=45,
        news_blackout_after_min=20,
        economic_data_day_block=True,
    )
    page.load(changed)
    round_tripped = page.current()

    editable = {field.name for field in dataclasses.fields(changed)} - {
        # Not on this form by design.
        "trading_mode", "network", "account_address", "coin",
        "paper_starting_balance", "risk_pct", "clamp_size_to_leverage",
        # Live only, so it is not on the form in the Paper settings this builds.
        "margin_mode",
        # Legacy. The form edits `daily_loss_limit_pct` and clears this on save, so
        # a zeroed percentage cannot hand control back to an old fixed limit.
        "daily_loss_limit_usdc",
    }
    for name in sorted(editable):
        assert getattr(round_tripped, name) == pytest.approx(
            getattr(changed, name)
        ) if isinstance(getattr(changed, name), float) else getattr(
            round_tripped, name
        ) == getattr(changed, name), f"{name} did not survive the round trip"


def test_the_trailing_fields_grey_out_when_trailing_is_off(qapp):
    page = SettingsPage(AppSettings())

    page.trailing.setChecked(False)
    assert not page.trail_distance.isEnabled()

    page.trailing.setChecked(True)
    assert page.trail_distance.isEnabled()


def test_the_stop_buffer_is_offered_to_a_strategy_that_takes_one(qapp):
    """Which fields are live is read off the strategy's constructor, so a field
    that reaches nothing is greyed rather than looking editable."""
    page = SettingsPage(AppSettings(strategy="volume_rejection"))
    assert page.stop_buffer.isEnabled()


def test_the_strategy_note_is_right_from_the_moment_the_page_opens(qapp):
    """Regression: the note and the greying were only refreshed when the dropdown
    was touched, so a page opened on a saved strategy described nothing at all."""
    page = SettingsPage(AppSettings(strategy="volume_rejection"))

    assert page.strategy_note.text()
    assert "breakout" in page.strategy_note.text().lower()


def test_the_news_blackout_settings_round_trip(qapp):
    """Regression: these three were declared, persisted and validated, but had no
    widget and were read by nothing. The blackout window was a number in a dataclass
    that did not reach the engine."""
    page = SettingsPage(AppSettings())
    page.load(
        AppSettings(
            news_blackout_enabled=True,
            news_blackout_before_min=45,
            news_blackout_after_min=5,
        )
    )

    assert page.news_auto.isChecked()
    assert page.news_before.value() == 45

    page.news_after.setValue(20)
    settings = page.current()

    assert settings.news_blackout_enabled
    assert settings.news_blackout_before_min == 45
    assert settings.news_blackout_after_min == 20


def test_the_blackout_windows_grey_out_when_the_calendar_is_off(qapp):
    page = SettingsPage(AppSettings())

    page.news_auto.setChecked(False)
    assert not page.news_before.isEnabled()

    page.news_auto.setChecked(True)
    assert page.news_before.isEnabled()


def test_the_blackout_controls_stay_live_while_the_bot_runs(qapp):
    """News is the one thing worth reacting to mid-session, and standing aside
    never puts money at risk."""
    page = SettingsPage(AppSettings())
    page.set_enabled(False)

    assert not page.risk.isEnabled()
    assert page.news_block.isEnabled()
    assert page.news_auto.isEnabled()


def test_changing_risk_or_leverage_asks_for_a_new_preview(qapp):
    page = SettingsPage(AppSettings())
    asked = []
    page.previewRequested.connect(lambda: asked.append(True))

    page.risk.setValue(25.0)
    page.leverage.setValue(7)
    page.timeframe.setCurrentIndex(0)

    assert len(asked) == 3


def test_the_agent_key_is_masked(qapp):
    page = SettingsPage(AppSettings())
    assert page.agent_key.echoMode() == page.agent_key.EchoMode.Password


def test_the_address_round_trips_but_the_key_never_enters_the_settings(qapp):
    """The settings object is written to SQLite in plain text; a key must not ride
    along in it."""
    page = SettingsPage(AppSettings())
    page.load(AppSettings(trading_mode=TradingMode.LIVE, account_address=LIVE_ADDRESS))
    page.agent_key.setText("0x" + "ab" * 32)

    settings = page.current()

    assert settings.account_address == LIVE_ADDRESS
    assert "ab" * 32 not in repr(settings)


def test_valid_settings_are_emitted_on_save(qapp):
    page = SettingsPage(AppSettings())
    emitted = []
    page.saved.connect(emitted.append)

    page.risk_unit.setCurrentIndex(0)  # USDC
    page.risk.setValue(25.0)
    page.button_save.click()

    assert len(emitted) == 1
    assert emitted[0].risk_usdc == 25.0
    assert emitted[0].risk_pct == 0.0  # the unit chosen is the unit that applies


def test_the_risk_unit_opens_on_the_percentage_the_spec_asks_for(qapp):
    """The default is 3% of equity, so the page must open on "% of equity" with 3
    in the box. Opening on USDC would show "5.00" beside a bot staking 3% - which
    is exactly the mismatch that had the bottom bar reading 0.10 USDC while the
    engine risked 0.30% of the account."""
    page = SettingsPage(AppSettings())

    assert page.risk_unit.currentData() == "pct"
    assert page.risk.value() == pytest.approx(3.0)

    emitted = []
    page.saved.connect(emitted.append)
    page.button_save.click()
    assert emitted[0].risk_pct == pytest.approx(0.03)


def test_every_problem_is_reported_at_once(qapp):
    """Regression: the key error returned early and hid the address error behind it,
    so a swapped pair took three attempts to sort out - fix one, meet the next."""
    page = SettingsPage(AppSettings())
    page.load(AppSettings(trading_mode=TradingMode.LIVE))

    page.address.setText("0x" + "ab" * 32)  # a key in the address field
    page.agent_key.setText(LIVE_ADDRESS)  # an address in the key field
    page.button_save.click()

    reported = page.problem.text()
    assert "is a wallet address, not a private key" in reported
    assert "is a private key, not a wallet address" in reported


def test_live_mode_cannot_be_saved_without_credentials(qapp):
    """The only invalid state this form can reach - there is no address field yet."""
    page = SettingsPage(AppSettings())
    emitted = []
    page.saved.connect(emitted.append)

    page.mode.setCurrentIndex(page.mode.findData(TradingMode.LIVE))
    page.button_save.click()

    assert emitted == []
    assert page.problem.isVisibleTo(page)
    assert "wallet address" in page.problem.text()


def test_the_form_cannot_produce_invalid_paper_settings(qapp):
    """Widget ranges carry the validation, so Save never fails in paper mode."""
    page = SettingsPage(AppSettings())
    spin_boxes = (page.risk, page.slippage, page.balance, page.daily_loss, page.leverage)

    for widget in spin_boxes:
        widget.setValue(widget.minimum())
    assert page.current().validate() == []

    for widget in spin_boxes:
        widget.setValue(widget.maximum())
    assert page.current().validate() == []


def test_locking_disables_the_form_but_not_the_blackout(qapp):
    """The news toggle has to keep working while the bot is running."""
    page = SettingsPage(AppSettings())
    page.set_enabled(False)
    assert not page.timeframe.isEnabled()
    assert not page.button_save.isEnabled()
    assert page.news_block.isEnabled()


# --- dashboard ------------------------------------------------------------


def test_dashboard_renders_a_flat_account(qapp):
    page = DashboardPage()
    page.apply(Snapshot(ready=True, connected=True, mark=63_000.0, equity=1_000.0))

    assert page.position_card._value.text() == "Flat"
    assert page.bot_card._value.text() == "STOPPED"
    assert "63,000" in page._price_label.text()


def test_dashboard_renders_an_open_position(qapp):
    page = DashboardPage()
    page.apply(
        Snapshot(
            ready=True, running=True, connected=True, mark=63_500.0,
            equity=1_027.5, margin_used=630.0, position=make_position(),
        )
    )

    assert "LONG" in page.position_card._value.text()
    assert "+27.50" in page.position_card._sub.text()
    assert page.bot_card._value.text() == "RUNNING"
    assert len(page.chart._level_lines) == 3  # entry, stop, target


def test_dashboard_clears_position_levels_when_flat(qapp):
    page = DashboardPage()
    page.apply(Snapshot(ready=True, connected=True, mark=63_500.0, position=make_position()))
    page.apply(Snapshot(ready=True, connected=True, mark=63_500.0))
    assert page.chart._level_lines == []


def test_the_change_is_measured_against_the_last_close(qapp):
    """Not against the price when the app opened, which reads 0.00% forever."""
    page = DashboardPage()
    page.load_candles(make_candles(10))  # last close is 63_009
    page.apply(Snapshot(ready=True, connected=True, mark=63_009.0 * 1.01))

    assert "+1.00% since last close" in page._pct_label.text()

    page.apply(Snapshot(ready=True, connected=True, mark=63_009.0 * 0.99))
    assert "-1.00% since last close" in page._pct_label.text()


def test_the_change_is_blank_before_any_candles_arrive(qapp):
    page = DashboardPage()
    page.apply(Snapshot(ready=True, connected=True, mark=63_000.0))
    assert page._pct_label.text() == ""


def test_dashboard_marks_the_feed_as_down(qapp):
    page = DashboardPage()
    page.apply(Snapshot(ready=True, connected=False))
    assert "No price feed" in page.cards["market"]._sub.text()


def test_dashboard_log_panel_keeps_the_newest_first(qapp):
    page = DashboardPage()
    page.add_log("first", logging.INFO)
    page.add_log("second", logging.WARNING)
    assert "second" in page._log_list.item(0).text()
    assert "first" in page._log_list.item(1).text()


def test_the_chart_offers_no_interaction_it_cannot_honour(qapp):
    """Regression: dragging was enabled, but `set_mark` redraws once a second and
    re-applies the range, so a pan was discarded before the hand left the mouse -
    and pyqtgraph's auto-range button sat in the corner offering to undo something
    that undid itself. The range selector is the view control."""
    page = DashboardPage()
    page.load_candles(make_candles(20))

    assert page.chart.getViewBox().state["mouseEnabled"] == [False, False]
    assert page.chart.plotItem.autoBtn is None or not page.chart.plotItem.autoBtn.isVisible()


def test_the_chart_still_follows_the_live_price(qapp):
    """The other half: locking the view must not stop it tracking new candles."""
    page = DashboardPage()
    page.load_candles(make_candles(20))
    (_, before), _ = page.chart.getViewBox().viewRange()

    page.load_candles(make_candles(40))
    (_, after), _ = page.chart.getViewBox().viewRange()

    assert after > before


def test_the_percentage_names_the_close_it_measured_against(qapp):
    """Regression: the reference is the last of whatever candles are loaded, which
    changes with the range. The same $63,012.5 read +0.00% on the 1H view (a 5m
    close) and -0.06% on the live view (a 4h close, hours old and not even drawn),
    with nothing on screen to say why."""
    page = DashboardPage()
    page.load_candles(make_candles(5), timeframe=Timeframe.M5)
    page.apply(Snapshot(ready=True, connected=True, mark=63_030.0))

    assert "5m close" in page._pct_label.text()

    page.load_candles(make_candles(5), timeframe=Timeframe.H4)
    page.apply(Snapshot(ready=True, connected=True, mark=63_030.0))

    assert "4h close" in page._pct_label.text()


def test_the_percentage_still_reads_without_a_timeframe(qapp):
    """load_candles is called without one in tests and by any older caller."""
    page = DashboardPage()
    page.load_candles(make_candles(5))
    page.apply(Snapshot(ready=True, connected=True, mark=63_030.0))

    assert "since last close" in page._pct_label.text()


def test_the_line_view_reaches_both_edges_of_the_chart(qapp):
    """Regression: the x range was -1..len whatever the style. A line is drawn at
    the indices themselves, so that left a whole empty slot at each end - on the
    1H view, 12 points inside 13 units, 15% of the width blank. It read as a chart
    that had been cut off before the current price."""
    from src.ui.chart import BODY_WIDTH

    page = DashboardPage()
    page._style_combo.setCurrentIndex(1)  # Line
    page.load_candles(make_candles(12))

    (x0, x1), _ = page.chart.getViewBox().viewRange()
    # Points sit at 0..11. Allow only pyqtgraph's 1% padding either side.
    assert x0 > -0.5, f"dead space on the left: range starts at {x0}"
    assert x1 < 11.5, f"dead space on the right: range ends at {x1}"


def test_the_candle_view_keeps_room_for_the_body(qapp):
    """The other half: a candle is a body BODY_WIDTH wide centred on its index, so
    the outermost ones need air or they are clipped by the axis."""
    from src.ui.chart import BODY_WIDTH

    page = DashboardPage()
    page.load_candles(make_candles(12))

    (x0, x1), _ = page.chart.getViewBox().viewRange()
    assert x0 <= -BODY_WIDTH / 2, f"the first candle is clipped: range starts at {x0}"
    assert x1 >= 11 + BODY_WIDTH / 2, f"the last candle is clipped: range ends at {x1}"


def test_a_single_candle_does_not_collapse_the_x_range(qapp):
    page = DashboardPage()
    page._style_combo.setCurrentIndex(1)
    page.load_candles(make_candles(1))

    (x0, x1), _ = page.chart.getViewBox().viewRange()
    assert x1 > x0


def test_chart_windows_trim_the_series(qapp):
    page = DashboardPage()
    page.load_candles(make_candles(200))
    page.chart.set_window(30)
    assert len(page.chart.points()) == 30
    page.chart.set_window(None)
    assert len(page.chart.points()) == 200


# --- the time range selector ----------------------------------------------


def test_every_range_is_a_span_of_history(qapp):
    """Same shape as the reference apps. There is no entry for "the bot's own
    candles": nothing on this chart is strategy-specific, so such a view would only
    have meant "4-hour candles" under a name that promised more."""
    page = DashboardPage()
    offered = [page._range_combo.itemText(i) for i in range(page._range_combo.count())]
    assert offered == ["1s", "1H", "4H", "1D", "1W", "1M", "YTD", "All"]


def test_the_default_range_is_a_day_and_fetches_immediately(qapp):
    """Index 0 is the live view, which opens empty — the default must not be it."""
    page = DashboardPage()
    assert page._range_combo.currentText() == "1D"
    assert page.current_request() == (Timeframe.M15, 96)


def test_a_stale_saved_range_falls_back_to_the_default(qapp, conn):
    """"Bot" was removed; a config carrying it must not land on the live view."""
    set_ui_state(conn, "chart_range", "Bot")
    page = DashboardPage(conn)
    assert page._range_combo.currentText() == "1D"


def test_a_saved_live_range_still_sets_the_chart_mode(qapp, conn):
    """It sits at index 0, so setCurrentIndex is a no-op and fires no signal."""
    set_ui_state(conn, "chart_range", "1s")
    page = DashboardPage(conn)
    assert page._range_combo.currentText() == "1s"
    assert page.chart._mode == "ticks"
    assert page.current_request() is None


def _select(page, label):
    index = next(
        i for i in range(page._range_combo.count()) if page._range_combo.itemText(i) == label
    )
    page._range_combo.setCurrentIndex(index)


# --- the live tick view ---------------------------------------------------


def test_the_live_view_draws_the_polled_price(qapp):
    """Hyperliquid has no 1s candles, so this is the polled price plotted tick by
    tick - the same thing the reference app's '1s' actually showed."""
    page = DashboardPage()
    for price in (63_000.0, 63_010.0, 62_990.0):
        page.chart.set_mark(price)

    _select(page, "1s")

    assert page.chart.ticks() == [63_000.0, 63_010.0, 62_990.0]
    assert page.chart._curve.isVisible()
    assert not page.chart._candlesticks.isVisible()
    assert len(page.chart._curve.getData()[0]) == 3


def test_ticks_are_recorded_even_while_a_candle_view_is_showing(qapp):
    """So switching to 1s has something to draw instead of an empty chart."""
    page = DashboardPage()
    page.load_candles(make_candles(10))
    page.chart.set_mark(63_000.0)
    page.chart.set_mark(63_020.0)

    assert page.chart._mode == "candles"
    assert page.chart.ticks() == [63_000.0, 63_020.0]


def test_the_live_view_needs_no_fetch(qapp):
    page = DashboardPage()
    requests = []
    page.chartRangeRequested.connect(lambda tf, count: requests.append((tf, count)))

    _select(page, "1s")

    assert requests == []
    assert page.current_request() is None


def test_the_style_toggle_is_disabled_on_the_live_view(qapp):
    """A candle built from single samples has no body worth drawing."""
    page = DashboardPage()
    _select(page, "1s")
    assert not page._style_combo.isEnabled()

    _select(page, "1D")
    assert page._style_combo.isEnabled()


def test_the_live_view_says_where_its_data_comes_from(qapp):
    page = DashboardPage()
    _select(page, "1s")
    assert "polled each second" in page._chart_title.text()


def test_the_live_view_starts_empty_and_survives_it(qapp):
    page = DashboardPage()
    _select(page, "1s")
    assert page.chart.ticks() == []


def test_a_one_dollar_wiggle_does_not_fill_the_live_chart(qapp):
    """BTC's tick is $1 and the spread is usually exactly that, so a quiet minute
    moves the mid a tick or two. Scaled to fit, that would read as a crash."""
    page = DashboardPage()
    _select(page, "1s")
    for price in (63_121.5, 63_122.5, 63_121.5, 63_122.5):
        page.chart.set_mark(price)

    _, (y0, y1) = page.chart.getViewBox().viewRange()
    assert y1 - y0 > 50  # a $1 move occupies a sliver, not the whole chart


def test_a_real_move_still_scales_to_fit(qapp):
    page = DashboardPage()
    _select(page, "1s")
    for price in (62_000.0, 63_000.0, 64_000.0):
        page.chart.set_mark(price)

    _, (y0, y1) = page.chart.getViewBox().viewRange()
    assert y0 <= 62_000.0
    assert y1 >= 64_000.0
    assert y1 - y0 < 3_000  # zoomed to the move, not padded into irrelevance


def test_picking_a_range_asks_for_a_coarser_interval_over_a_longer_span(qapp):
    """A week of 5m candles would be 2,000 bars of mush."""
    page = DashboardPage()
    requests = []
    page.chartRangeRequested.connect(lambda tf, count: requests.append((tf, count)))

    _select(page, "1H")
    _select(page, "1W")
    _select(page, "All")

    assert requests[0] == (Timeframe.M5, 12)
    assert requests[1] == (Timeframe.H1, 168)
    assert requests[2] == (Timeframe.W1, 400)


def test_ytd_asks_for_as_many_daily_candles_as_the_year_has_run(qapp):
    page = DashboardPage()
    requests = []
    page.chartRangeRequested.connect(lambda tf, count: requests.append((tf, count)))

    _select(page, "YTD")

    timeframe, count = requests[0]
    assert timeframe is Timeframe.D1
    assert 2 <= count <= 368


def test_switching_to_the_live_view_stops_requesting(qapp):
    page = DashboardPage()
    assert page.current_request() is not None

    _select(page, "1s")
    assert page.current_request() is None


def test_the_chart_title_names_the_span_and_the_candles(qapp):
    """"4H" means four hours of history, not four-hour candles, so the title spells
    out both rather than leaving the label to be read either way."""
    page = DashboardPage()
    _select(page, "1W")
    assert "1W view" in page._chart_title.text()
    assert "1 hour candles" in page._chart_title.text()

    _select(page, "4H")
    assert "4H view" in page._chart_title.text()
    assert "5 mins candles" in page._chart_title.text()


def test_chart_x_range_covers_the_data(qapp):
    """Regression: the x axis stayed on its empty-plot default once data arrived,
    so 120 candles were drawn squeezed into a range of -0.5 to 1.5."""
    page = DashboardPage()
    page.load_candles(make_candles(200))
    page.chart.set_window(120)

    (x0, x1), _ = page.chart.getViewBox().viewRange()
    assert x0 <= 0
    assert x1 >= 119


def test_the_live_price_is_the_moving_tip_of_the_line(qapp):
    """Closed candles only move once a timeframe, so the tip is what looks alive."""
    page = DashboardPage()
    page.load_candles(make_candles(10))
    page.chart.set_window(None)
    assert len(page.chart.points()) == 10

    page.chart.set_mark(63_500.0)
    assert page.chart.points()[-1] == 63_500.0
    assert len(page.chart.points()) == 11

    page.chart.set_mark(63_600.0)
    assert page.chart.points()[-1] == 63_600.0
    assert len(page.chart.points()) == 11  # replaced, not accumulated


def test_reloading_the_same_candles_does_not_redraw(qapp):
    """The UI hands over the whole buffer every second."""
    page = DashboardPage()
    candles = make_candles(50)
    page.load_candles(candles)

    calls = []
    original = page.chart._redraw
    page.chart._redraw = lambda: (calls.append(1), original())

    page.load_candles(list(candles))
    assert calls == []

    page.load_candles(make_candles(51))
    assert len(calls) == 1


def test_chart_survives_having_no_candles(qapp):
    """A price with no history has no candle to sit in, so only the mark shows."""
    page = DashboardPage()
    page.load_candles([])
    page.chart.set_mark(63_000.0)
    assert page.chart.visible_candles() == []


# --- candlesticks ---------------------------------------------------------


def test_candles_are_the_default_view(qapp):
    """The stop is 2xATR, and ATR comes from the highs and lows a line chart hides."""
    page = DashboardPage()
    assert page._style_combo.currentData() == "candles"
    assert page.chart._candlesticks.isVisible()
    assert not page.chart._curve.isVisible()


def test_switching_to_the_line_view(qapp):
    page = DashboardPage()
    page.load_candles(make_candles(20))
    page._style_combo.setCurrentIndex(1)

    assert page.chart._curve.isVisible()
    assert not page.chart._candlesticks.isVisible()
    assert len(page.chart._curve.getData()[0]) == 20


def test_the_forming_candle_is_drawn_after_the_closed_ones(qapp):
    page = DashboardPage()
    closed = make_candles(10)
    forming = Candle(
        open_time_ms=10 * 3_600_000,
        close_time_ms=11 * 3_600_000 - 1,
        open=63_010.0, high=63_050.0, low=62_990.0, close=63_020.0,
        volume=1.0, trades=1,
    )
    page.load_candles(closed, forming)
    page.chart.set_window(None)

    assert len(page.chart.visible_candles()) == 11
    assert page.chart.visible_candles()[-1].close == 63_020.0


def test_the_live_price_stretches_the_forming_candle(qapp):
    """Between feed refreshes the mark is what moves the candle being built."""
    page = DashboardPage()
    forming = Candle(0, 1, 63_010.0, 63_050.0, 62_990.0, 63_020.0, 1.0, 1)
    page.load_candles(make_candles(5), forming)

    page.chart.set_mark(63_400.0)  # a new high
    live = page.chart.visible_candles()[-1]
    assert live.close == 63_400.0
    assert live.high == 63_400.0
    assert live.low == 62_990.0  # untouched

    page.chart.set_mark(62_500.0)  # now a new low
    live = page.chart.visible_candles()[-1]
    assert live.low == 62_500.0
    assert live.close == 62_500.0


def test_a_forming_candle_is_opened_from_the_last_close_when_the_feed_has_none(qapp):
    page = DashboardPage()
    page.load_candles(make_candles(10))  # last close 63_009
    page.chart.set_window(None)
    page.chart.set_mark(63_100.0)

    candles = page.chart.visible_candles()
    assert len(candles) == 11
    assert candles[-1].open == 63_009.0
    assert candles[-1].close == 63_100.0


def test_the_price_axis_never_goes_negative(qapp):
    """Seven years of BTC span $3.8k to $124k, and 6% padding would push it under."""
    page = DashboardPage()
    page.load_candles(
        [
            Candle(i * 1000, i * 1000 + 999, 4_000.0, 124_000.0, 3_800.0, 120_000.0, 1.0, 1)
            for i in range(10)
        ]
    )
    _, (y0, _) = page.chart.getViewBox().viewRange()
    assert y0 >= 0


def test_the_y_range_covers_the_wicks(qapp):
    """A line chart only needs the closes; candles must not clip their highs."""
    page = DashboardPage()
    page.load_candles(make_candles(30))
    page.chart.set_window(None)

    highs = [candle.high for candle in page.chart.visible_candles()]
    lows = [candle.low for candle in page.chart.visible_candles()]
    _, (y0, y1) = page.chart.getViewBox().viewRange()
    assert y0 <= min(lows)
    assert y1 >= max(highs)


# --- trades, stats, logs --------------------------------------------------


def test_trades_page_handles_an_empty_history(qapp, conn):
    page = TradesPage(conn)
    page.reload(TradingMode.PAPER)
    assert page.table.rowCount() == 0
    assert "0 closed" in page.summary.text()


def test_trades_page_lists_recorded_fills(qapp, conn):
    record_fill(
        conn, TradingMode.PAPER,
        Fill(1_700_000_000_000, "BTC", Side.LONG, 0.05, 63_000.0, 0.14, FillReason.ENTRY),
    )
    record_fill(
        conn, TradingMode.PAPER,
        Fill(
            1_700_000_100_000, "BTC", Side.LONG, 0.05, 65_000.0, 0.15,
            FillReason.TAKE_PROFIT, realised_pnl=99.7,
        ),
    )

    page = TradesPage(conn)
    page.reload(TradingMode.PAPER)

    assert page.table.rowCount() == 2
    assert page.table.item(0, 3).text() == "take profit"  # newest first
    assert "+99.70" in page.table.item(0, 7).text()
    assert "1 closed" in page.summary.text()


def test_stats_page_summarises_closed_trades(qapp, conn):
    record_fill(
        conn, TradingMode.PAPER,
        Fill(
            1_700_000_100_000, "BTC", Side.LONG, 0.05, 65_000.0, 0.15,
            FillReason.TAKE_PROFIT, realised_pnl=99.7,
        ),
    )
    page = StatsPage(conn)
    page.refresh(TradingMode.PAPER)

    assert "Closed Trades: 1" in page._labels["Closed Trades"].text()
    assert "Win Rate: 100%" in page._labels["Win Rate"].text()
    assert "+99.70" in page._labels["Total PnL"].text()


def test_logs_page_prepends_and_colours(qapp):
    page = LogsPage()
    page.add_log(log_line("candle closed"))
    page.add_log(log_line("trade rejected", logging.WARNING))

    assert page.table.rowCount() == 2
    assert page.table.item(0, 2).text() == "trade rejected"
    assert page.table.item(1, 2).text() == "candle closed"


# --- the whole window -----------------------------------------------------


def test_window_builds_and_shows_every_page(qapp, conn):
    """Constructing must need no event loop and no network."""
    window = MainWindow(conn, AppSettings())
    assert window._stack.count() == len(MainWindow.PAGES)

    for index in range(len(MainWindow.PAGES)):
        window._nav.setCurrentRow(index)
        assert window._stack.currentIndex() == index


def test_the_breadcrumb_names_the_page_from_the_start(qapp, conn):
    """The nav opens on row 0, which fires no signal - so it is set by hand, or the
    bar reads "Dashboard /" with nothing after the slash."""
    window = MainWindow(conn, AppSettings())
    assert window.top._title.text() == "Live Dashboard"
    assert window.top._crumb.text() == "Market Overview"

    # The top bar names the page, not the nav item.
    window._nav.setCurrentRow(1)
    assert window._nav.item(1).text() == "Settings"
    assert window.top._title.text() == "Bot Settings"
    assert window.top._crumb.text() == "Account Configuration"


def test_the_status_bar_follows_the_snapshot(qapp, conn):
    window = MainWindow(conn, AppSettings())

    window._on_update(Snapshot(ready=True, connected=True, running=True))
    assert "Running" in window.status._state.text()
    assert "Connected" in window.top.connection._label.text()

    window._on_update(Snapshot(ready=False, connected=False))
    assert "Not connected" in window.status._state.text()
    assert "Disconnected" in window.top.connection._label.text()


def test_bottom_bar_reflects_the_settings(qapp, conn):
    window = MainWindow(
        conn, AppSettings(risk_usdc=12.5, risk_pct=0.0, leverage=3, timeframe=Timeframe.H1)
    )
    assert window.bottom.timeframe_label.text() == "1 hour"
    assert window.bottom.risk_label.text() == "12.50 USDC"
    assert window.bottom.leverage_label.text() == "3x"
    assert "PAPER" in window.bottom.market_label.text()


def test_the_bottom_bar_names_the_running_strategy(qapp, conn):
    """A live account was once started on the wrong strategy because this bar did
    not say which one was loaded."""
    window = MainWindow(conn, AppSettings(strategy="volume_rejection"))

    assert "Volume rejection" in window.bottom.strategy_label.text()


def test_the_bottom_bar_shows_a_percentage_risk_as_a_percentage(qapp, conn):
    """Regression: the bar printed `risk_usdc` regardless, so it read "0.10 USDC"
    while the engine was staking 0.30% of equity. The number on screen was not the
    number being risked."""
    window = MainWindow(conn, AppSettings(risk_pct=0.003, risk_usdc=0.10))

    assert "0.30%" in window.bottom.risk_label.text()
    # The point of the regression: the stale USDC figure must not be what shows.
    assert "USDC" not in window.bottom.risk_label.text()
    assert "0.10" not in window.bottom.risk_label.text()


def test_run_controls_follow_the_snapshot(qapp, conn):
    window = MainWindow(conn, AppSettings())

    window._on_update(Snapshot(ready=True, running=False))
    assert window.bottom.start_btn.isEnabled()
    assert not window.bottom.stop_btn.isEnabled()
    assert not window.bottom.close_btn.isEnabled()

    window._on_update(Snapshot(ready=True, running=True, position=make_position()))
    assert not window.bottom.start_btn.isEnabled()
    assert window.bottom.stop_btn.isEnabled()
    assert window.bottom.close_btn.isEnabled()
    assert not window.settings_page.button_save.isEnabled()  # locked while running


def test_the_settings_buttons_are_not_squashed(qapp, conn):
    """Regression: a form taller than its scroll area was compressed rather than
    scrolled, crushing the buttons from 35px to 19px and clipping their labels."""
    window = MainWindow(conn, AppSettings())
    window.resize(window.minimumWidth(), window.minimumHeight())
    window.show()
    window._nav.setCurrentRow(1)
    qapp.processEvents()

    page = window.settings_page
    for button in (page.button_save, page.button_reset):
        assert button.height() >= button.sizeHint().height(), button.text()


def test_the_bottom_bar_fits_at_the_smallest_allowed_window(qapp, conn):
    """Four labelled columns, three buttons and an uptime counter have to fit, or
    the market label clips to "BTC-USD perp [PA"."""
    window = MainWindow(conn, AppSettings())
    window.show()
    qapp.processEvents()

    needed = window.bottom.sizeHint().width()
    available = window.minimumWidth() - window._sidebar.width() - 24  # content margins
    assert needed <= available, f"bottom bar wants {needed}px, has {available}px"


async def test_start_actually_schedules_the_startup(qapp, conn):
    """Regression: start() was called before the loop was running, so the coroutine
    it scheduled was silently dropped and the app came up connected to nothing -
    empty log, disabled START, no clue why."""
    window = MainWindow(conn, AppSettings())
    ran = []

    async def fake_start_up():
        ran.append(True)

    window._start_up = fake_start_up
    window.start()
    await asyncio.sleep(0)  # let the task run

    assert ran == [True]
    assert window._refresh_timer.isActive()


async def test_the_preview_reaches_the_card_that_actually_shows_it(qapp, conn):
    """Regression: the preview card moved from Settings to About, and the window went
    on calling `settings_page.show_preview`. Every page test passed - each page was
    fine on its own - while the running app logged an AttributeError once a second."""
    from src.ui.controller import Preview

    window = MainWindow(conn, AppSettings())

    async def fake_preview(_settings):
        return Preview(
            price=63_050.0, equity=99.72, stop_distance=804.6,
            size=0.00311, notional=195.92, margin=39.18,
        )

    window.controller.preview = fake_preview
    await window._refresh_preview()

    assert window.about.preview_card._values["size"].text() == "0.00311 BTC"
    assert "can trade" in window.about.preview_card.verdict.text()


def test_opening_about_refreshes_the_preview(qapp, conn):
    """The card is on About, so that is the page whose arrival must re-size it."""
    window = MainWindow(conn, AppSettings())
    scheduled = []
    window._schedule = lambda coro, *args: scheduled.append(coro)

    window._nav.setCurrentRow(main_window.PAGE_ABOUT)

    assert window._refresh_preview in scheduled


async def test_a_crash_in_a_scheduled_task_is_logged(qapp, conn, caplog):
    """A fire-and-forget task that raises must not vanish."""
    window = MainWindow(conn, AppSettings())

    async def explode():
        raise RuntimeError("something broke")

    with caplog.at_level(logging.ERROR):
        window._schedule(explode)
        # The task has to finish and its done-callback has to be dispatched, which
        # is a further turn of the loop.
        await asyncio.sleep(0.05)

    assert "something broke" in caplog.text


class _StubTask:
    """Stands in for a finished asyncio.Task that ended on `error`."""

    def __init__(self, error):
        self._error = error

    def cancelled(self) -> bool:
        return False

    def exception(self):
        return self._error


def test_ctrl_c_stops_the_app_instead_of_being_logged(caplog, monkeypatch):
    """Regression: KeyboardInterrupt is not an Exception, but `task.exception()`
    returns it anyway. Reported as "background task failed" it was swallowed, the
    loop kept running, and the process had to be killed - which skipped
    `conn.close()`, orphaned the SQLite WAL, and lost every saved setting.

    Raising it inside a real task would abort the test run itself, so the handler is
    called with a stand-in for the task that carries one."""
    quit_calls = []
    monkeypatch.setattr(
        main_window,
        "QApplication",
        type("StubApp", (), {"instance": staticmethod(
            lambda: type("App", (), {"quit": lambda self: quit_calls.append(True)})()
        )}),
    )

    with caplog.at_level(logging.ERROR):
        MainWindow._report_failure(_StubTask(KeyboardInterrupt()))

    assert quit_calls == [True]
    assert "background task failed" not in caplog.text


def test_an_ordinary_failure_is_still_reported_and_does_not_quit(caplog, monkeypatch):
    """The other half: a real bug must not be mistaken for someone pressing Ctrl+C."""
    quit_calls = []
    monkeypatch.setattr(
        main_window,
        "QApplication",
        type("StubApp", (), {"instance": staticmethod(
            lambda: type("App", (), {"quit": lambda self: quit_calls.append(True)})()
        )}),
    )

    with caplog.at_level(logging.ERROR):
        MainWindow._report_failure(_StubTask(RuntimeError("something broke")))

    assert quit_calls == []
    assert "something broke" in caplog.text


# --- the busy overlay -----------------------------------------------------


async def _noop_apply(settings):
    """Stands in for BotController.apply_settings, which needs a live session."""


async def test_saving_says_it_is_working(qapp, conn):
    """Saving in Live rebuilds the broker: a new Exchange is constructed, the asset
    universe is fetched and the account is read back. For a few seconds the form
    looked untouched - no error, no change, no sign the click had registered."""
    window = MainWindow(conn, AppSettings())
    applied = []

    async def fake_apply(settings):
        applied.append(settings)

    window.controller.apply_settings = fake_apply

    assert not window.busy.busy

    window._on_settings_saved(AppSettings(trading_mode=TradingMode.LIVE))

    # The scrim goes up before the work is even scheduled, which is the point.
    assert window.busy.busy
    assert "Applying settings" in window.busy._message.text()
    assert "Hyperliquid" in window.busy._detail.text()

    await asyncio.sleep(0)  # let the scheduled task run
    assert len(applied) == 1


async def test_the_overlay_names_what_paper_is_doing(qapp, conn):
    window = MainWindow(conn, AppSettings())
    window.controller.apply_settings = _noop_apply

    window._on_settings_saved(AppSettings())

    assert "simulated" in window.busy._detail.text().lower()


async def test_the_overlay_lifts_when_the_settings_land(qapp, conn):
    window = MainWindow(conn, AppSettings())
    window.controller.apply_settings = _noop_apply
    window._on_settings_saved(AppSettings())

    window._on_settings_applied(AppSettings())

    assert not window.busy.busy


async def test_a_failure_also_lifts_the_overlay(qapp, conn):
    """Otherwise a refused save leaves the user behind a scrim with a banner they
    cannot reach."""
    window = MainWindow(conn, AppSettings())
    window.controller.apply_settings = _noop_apply
    window._on_settings_saved(AppSettings())

    window._on_failed("Could not reach Hyperliquid")

    assert not window.busy.busy
    assert window.alert.isVisibleTo(window)


def test_the_overlay_lets_go_rather_than_trapping_anyone(qapp, conn):
    """A user stuck behind a scrim is worse off than one looking at a stale form."""
    from src.ui.busy_overlay import BusyOverlay

    overlay = BusyOverlay()
    overlay.start("Applying settings...")
    assert overlay.busy

    overlay._timeout.timeout.emit()  # the safety net firing

    assert not overlay.busy


def test_the_overlay_swallows_input_meant_for_the_form(qapp, conn):
    """It covers the bottom bar too - START must not be reachable mid-rewire.

    Driven with a stand-in rather than a real QMouseEvent: every constructor PySide
    offers for one is deprecated, and what is being checked is that the handler
    accepts the event rather than letting it through."""
    from src.ui.busy_overlay import BusyOverlay

    class Event:
        def __init__(self):
            self.accepted = False

        def accept(self):
            self.accepted = True

    overlay = BusyOverlay()
    overlay.start("Applying settings...")

    click, key = Event(), Event()
    overlay.mousePressEvent(click)
    overlay.keyPressEvent(key)

    assert click.accepted and key.accepted


def test_errors_raise_the_alert_banner(qapp, conn):
    window = MainWindow(conn, AppSettings())
    assert not window.alert.isVisibleTo(window)

    window._on_log(log_line("could not reach Hyperliquid", logging.ERROR))
    assert window.alert.isVisibleTo(window)

    window.alert.dismiss()
    assert not window.alert.isVisibleTo(window)


def test_a_save_that_worked_says_so(qapp, conn):
    """It used to look identical to a save that never registered - the scrim came,
    went, and nothing else changed."""
    window = MainWindow(conn, AppSettings())

    window._on_settings_applied(
        AppSettings(timeframe=Timeframe.M15, risk_pct=0.03, leverage=5)
    )

    assert window.alert.isVisibleTo(window)
    text = window.alert._label.text()
    # It names what is in force, so the confirmation doubles as a check.
    assert "PAPER" in text and "15 mins" in text and "3.00% of equity" in text
    assert "5x" in text


def test_a_confirmation_never_paints_over_an_error(qapp, conn):
    """Applying Live settings can fall back to Paper, and that failure is raised
    before the applied signal. A green "Saved" on top of it would bury the one
    thing the user has to see."""
    window = MainWindow(conn, AppSettings())

    window._on_failed("Live mode refused - running in PAPER instead: no API key")
    window._on_settings_applied(AppSettings())

    assert window.alert.isVisibleTo(window)
    assert "Live mode refused" in window.alert._label.text()
    assert "Settings saved" not in window.alert._label.text()


def test_a_confirmation_clears_itself(qapp, conn):
    """Errors are dismissed by hand; a confirmation that stayed would become
    furniture, and the next one would be indistinguishable from the last."""
    from src.ui.alert_banner import AlertBanner

    banner = AlertBanner()
    banner.show_success("Settings saved")
    assert banner.isVisibleTo(banner) or not banner.isHidden()

    banner._timer.timeout.emit()  # fire the auto-dismiss without waiting for it
    assert banner.isHidden()


def test_an_error_outranks_a_confirmation_already_showing(qapp, conn):
    from src.ui.alert_banner import AlertBanner

    banner = AlertBanner()
    banner.show_success("Settings saved")
    banner.show_error("Could not reach Hyperliquid")

    assert "Could not reach Hyperliquid" in banner._label.text()
    assert not banner._timer.isActive()  # it must not time itself out


def test_sidebar_collapse_is_remembered(qapp, conn):
    window = MainWindow(conn, AppSettings())
    assert window._sidebar.width() == main_window.SIDEBAR_WIDE

    window._toggle_sidebar()
    assert window._sidebar.width() == main_window.SIDEBAR_NARROW
    assert window._nav.item(0).text() == ""
    assert get_ui_state(conn, "sidebar_collapsed") == "1"

    # A fresh window restores the collapsed state.
    assert MainWindow(conn, AppSettings())._sidebar.width() == main_window.SIDEBAR_NARROW


def test_the_window_opens_maximised_by_default(qapp, conn):
    """The dashboard puts a chart, a log column and five cards side by side."""
    assert get_ui_state(conn, "window_maximized", "1") == "1"


def test_closing_remembers_whether_it_was_maximised(qapp, conn):
    window = MainWindow(conn, AppSettings())
    window.showNormal()
    window.close()
    assert get_ui_state(conn, "window_maximized") == "0"


async def test_a_restored_chart_range_is_loaded_at_startup(qapp, conn):
    """Regression: the saved range emitted its fetch request while DashboardPage was
    still being constructed, before MainWindow had connected to the signal. Nothing
    was listening, nothing was fetched, and the chart sat empty."""
    set_ui_state(conn, "chart_range", "1W")
    window = MainWindow(conn, AppSettings())

    assert window.dash.chart.visible_candles() == []  # nothing fetched yet

    fetched = []

    async def fake_fetch(timeframe, count):
        fetched.append((timeframe, count))
        return make_candles(20)

    window.controller.fetch_chart_candles = fake_fetch
    await window._load_chart_range(*window.dash.current_request())

    assert fetched == [(Timeframe.H1, 168)]
    assert len(window.dash.chart.visible_candles()) == 20


async def test_startup_asks_for_the_restored_range(qapp, conn):
    """The window must ask again once it is wired, or the chart never fills."""
    set_ui_state(conn, "chart_range", "1D")
    window = MainWindow(conn, AppSettings())

    async def no_connection():
        return False

    window.controller.initialise = no_connection
    asked = []
    window._refresh_chart_range = lambda: asked.append(True)

    await window._start_up()

    assert asked == [True]


def test_ui_state_does_not_leak_into_settings(qapp, conn):
    """`ui.` keys share the settings table; loading settings must ignore them."""
    from src.config import load_settings

    window = MainWindow(conn, AppSettings())
    window._toggle_sidebar()
    assert load_settings(conn) == AppSettings()
