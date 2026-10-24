"""Owner-facing compact report: values only, no setup tutorial or repeated hints."""
from account_details import account_identity
from risk_limits import risk_state
from recovery import short_summary, selected_state


def compact_report(repo, worker):
    d = repo.doc
    s = d['account_stats'][d['account_mode']]
    t = d['trading']
    identity = account_identity(d, worker.po)
    settled = s['won_trades'] + s['lost_trades']
    rate = s['won_trades'] / settled * 100 if settled else 0
    balance = f'${worker.balance:,.2f}' if worker.balance is not None else '—'
    risk = risk_state(d)
    limits = 'تقليدي' if risk['mode'] == 'TRADITIONAL' else f"ربح ${risk['profit_limit']:g} / خسارة ${risk['loss_limit']:g}"
    payout='—'
    if callable(getattr(worker,'payout_book',None)):
        book=worker.payout_book()
        value=book.rate(t['asset'],t['duration_seconds'],d['payout']['max_age_seconds']) if book else None
        if value is not None:payout=f'{value:g}%'
    status=worker.pause_text() if callable(getattr(worker,'pause_text',None)) else ('🟢 يعمل' if worker.running else '🔴 متوقف')
    lines = ['📊 إحصائيات التداول',
             f"{status} | {d['account_mode']} | UID: {identity['uid'] or '—'}",
             f"💱 {t['asset']} | ⏱ {t['duration_seconds']}ث | أساس ${t['trade_amount']:g}",
             '━━━━━━━━━━━━━━━━',
             f"الصفقات المحسومة: {s['won_trades']+s['lost_trades']+s['draws']:,} | النجاح: {rate:.1f}%",
             f"رابحة: {s['won_trades']:,} | خاسرة: {s['lost_trades']:,} | تعادل: {s['draws']:,}",
             f"سلاسل مستنفدة: {s['sequence_losses']:,} | خسارتها: ${s['lost_amount']:,.2f}",
             '━━━━━━━━━━━━━━━━',
             f'📈 عائد الزوج الحالي: {payout}',
             f"مبالغ الدخول: ${s['total_staked']:,.2f}",
             f"💰 صافي الربح / الخسارة: ${s['net_pnl']:+,.2f}",
             f'الرصيد: {balance}',
             short_summary(d), f'🛡 الحدود: {limits}']
    notes = []
    if s.get('unconfirmed') or worker.pending:
        notes.append(f"غير مؤكدة: {s.get('unconfirmed',0)} | معلّقة: {int(bool(worker.pending))}")
    state = selected_state(d)
    if state['active'] and state.get('uncertain'):
        notes.append('التعزيز يحتوي نتائج غير مؤكدة.')
    if notes:
        lines += ['⚠️ ' + ' · '.join(notes)]
    return '\n'.join(lines)
