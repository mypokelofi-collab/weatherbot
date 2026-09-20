"""Terminal report for a backtest run."""

from __future__ import annotations

from ..core.clock import iso


def _line(char: str = "─", n: int = 78) -> str:
    return char * n


def print_report(result: dict) -> None:
    rec = result["recording"]
    st = result["stats"]
    ex = result["execution"]
    pf = result["portfolio"]

    print(_line("═"))
    print(f" flowbot backtest · {rec['symbol']} {result['config']['interval']} · {rec['venue']}")
    print(_line("═"))
    print(f" data      {iso(rec['start'])} → {iso(rec['end'])}  ({rec['hours']}h, "
          f"{rec['trades']:,} prints, {rec['books']:,} book snapshots, {rec['size_mb']}MB)")
    print(f" bars      {result['bars_seen']} seen · {result['bars_available']} in the recording "
          f"· {result['signals']} signals evaluated")
    print(f" runtime   {result['wall_seconds']}s wall clock")
    print(_line())

    print(" RESULT")
    print(f"   equity          {pf['start_equity']:,.2f} → {pf['equity']:,.2f}"
          f"   ({pf['pnl']:+,.2f} / {pf['pnl_pct']:+.2f}%)")
    print(f"   trades          {st['trades']}  ({st['longs']} long / {st['shorts']} short)")
    print(f"   win rate        {st['win_rate']:.1f}%   profit factor {st['profit_factor']}")
    print(f"   expectancy      {st['expectancy_r']:+.3f}R per trade"
          f"   (avg win {st['avg_win_r']:+.2f}R / avg loss {st['avg_loss_r']:+.2f}R)")
    print(f"   best / worst    {st['best_r']:+.2f}R / {st['worst_r']:+.2f}R"
          f"   max losing streak {st['max_consecutive_losses']}")
    print(f"   max drawdown    {st['max_drawdown_pct']:.2f}%   return {st['return_pct']:+.2f}%")
    print(f"   exposure        {st['time_in_market_pct']:.1f}% of bars"
          f"   avg hold {st['avg_bars_held']:.1f} bars")
    print(_line())

    print(" COSTS (the part backtests usually lie about)")
    print(f"   fees paid       {st['fees']:,.2f}   "
          f"({ex['maker_fills']} maker / {ex['taker_fills']} taker fills)")
    print(f"   avg slippage    entry {st['avg_entry_slippage_bps']:+.2f}bps · "
          f"exit {st['avg_exit_slippage_bps']:+.2f}bps")
    print(f"   orders          {ex['submitted']} sent · {ex['filled']} filled · "
          f"{ex['rejected']} rejected · {ex['canceled']} cancelled")
    gross = st["net_pnl"] + st["fees"]
    if st["fees"]:
        print(f"   cost drag       fees are {st['fees'] / abs(gross) * 100:.1f}% of gross PnL"
              if gross else "   cost drag       n/a")
    print(_line())

    if result["trades"]:
        print(" TRADES")
        print(f"   {'#':>3} {'side':<5} {'entry':>10} {'exit':>10} {'pnl':>9} {'R':>7} "
              f"{'bars':>5}  why it closed")
        for t in result["trades"][-25:]:
            print(f"   {t['id']:>3} {t['side']:<5} {t['entry_price']:>10,.2f} "
                  f"{t['exit_price']:>10,.2f} {t['pnl']:>+9.2f} {t['r_multiple']:>+7.2f} "
                  f"{t['bars_held']:>5}  {t['exit_reason'][:38]}")
        print(_line())

    if result["warnings"]:
        print(" READ THIS BEFORE BELIEVING ANY OF THE ABOVE")
        for w in result["warnings"]:
            print(f"   ⚠ {w}")
        print(_line())
