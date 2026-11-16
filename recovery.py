"""Optional fixed-increment recovery layer above the original Martingale.
A phase begins only after an entire verified ladder loss. The raised base is
original base + increment ONCE; losing another raised ladder does not raise it
again. All arithmetic uses confirmed NET balance deltas, never turnover/payout
percentages. Per-account state is saved atomically with the resolved order.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math


def recovery_defaults():
    return dict(mode='TRADITIONAL', increment=1.0, exit_mode='RECOVER_LOSS', win_target=3)


def state_defaults(epoch=0):
    return dict(epoch=epoch, active=False, original_base=0.0, target_loss=0.0,
                phase_pnl=0.0, wins=0, uncertain=0, sequence_pnl=0.0,
                sequence_known=True, sequence_signature=None, last_event='')


def validate_recovery(settings, base):
    if not isinstance(settings, dict) or settings.get('mode') not in ('TRADITIONAL', 'ENABLED'):
        raise ValueError('وضع التعزيز غير صالح')
    if settings.get('exit_mode') not in ('RECOVER_LOSS', 'WINS'):
        raise ValueError('طريقة إنهاء التعزيز غير صالحة')
    increment = settings.get('increment')
    if isinstance(increment, bool):
        raise ValueError('زيادة التعزيز يجب أن تكون موجبة')
    try:
        value = float(increment)
    except (ValueError, TypeError):
        raise ValueError('زيادة التعزيز يجب أن تكون رقماً موجباً') from None
    if not math.isfinite(value) or not .01 <= value <= 100000:
        raise ValueError('زيادة التعزيز بين 0.01 و100000 دولار')
    if settings['mode'] == 'ENABLED' and float(base) + value > 100000:
        raise ValueError('المبلغ الأساسي مع الزيادة يجب ألا يتجاوز 100000 دولار')
    target = settings.get('win_target')
    if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= 1000:
        raise ValueError('عدد الانتصارات يجب أن يكون عدداً صحيحاً بين 1 و1000')
    return settings


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.000001')))


def reset_state(state, preserve_phase=False):
    epoch = int(state.get('epoch', 0)) + 1
    if not preserve_phase:
        return state_defaults(epoch)
    new = deepcopy(state)
    new.update(epoch=epoch, sequence_pnl=0.0, sequence_known=True, sequence_signature=None)
    return new


def selected_state(doc, mode=None):
    mode = mode or doc['account_mode']
    return {**state_defaults(), **doc['runtime'].get('recovery', {}).get(mode, {})}


def sizing(doc, mode=None):
    settings = {**recovery_defaults(), **doc.get('recovery', {})}
    state = selected_state(doc, mode)
    base = float(doc['trading']['trade_amount'])
    enabled = settings['mode'] == 'ENABLED'
    effective = round((state['original_base'] if state['active'] else base) +
                      (float(settings['increment']) if enabled and state['active'] else 0), 2)
    return effective, {'epoch': state['epoch'], 'base_amount': base,
                       'effective_base': effective, 'enabled': enabled}


def after_result(settings, state, order, result, before, after, seq_lost):
    state = {**state_defaults(), **deepcopy(state)}
    context = order.get('recovery_context')
    if not context or context.get('epoch') != state['epoch']:
        return state  # legacy/retired in-flight order must not resurrect a canceled phase
    if settings['mode'] != 'ENABLED' or not context.get('enabled'):
        return state
    if state['sequence_signature'] != order.get('sizing_signature'):
        state.update(sequence_signature=deepcopy(order.get('sizing_signature')),
                     sequence_pnl=0.0, sequence_known=True)
    b, a = number(before), number(after)
    delta = a-b if b is not None and a is not None and result in ('win','loss','draw') else None
    if delta is None:
        state['sequence_known'] = False
        if state['active']:
            state['uncertain'] += 1
    else:
        state['sequence_pnl'] = money(Decimal(str(state['sequence_pnl'])) + delta)
    if state['active']:
        if delta is not None:
            state['phase_pnl'] = money(Decimal(str(state['phase_pnl'])) + delta)
            if result == 'win':
                state['wins'] += 1
        done = (settings['exit_mode'] == 'RECOVER_LOSS' and state['uncertain'] == 0 and
                Decimal(str(state['phase_pnl'])) >= Decimal(str(state['target_loss']))) or (
                settings['exit_mode'] == 'WINS' and state['wins'] >= settings['win_target'])
        # No finishing on an unknown result even if settings changed meanwhile.
        if done and delta is not None:
            new = reset_state(state)
            new['last_event'] = 'RECOVERED' if settings['exit_mode'] == 'RECOVER_LOSS' else 'WIN_TARGET'
            return new
    elif seq_lost and state['sequence_known'] and state['sequence_pnl'] < 0:
        new = reset_state(state)
        new.update(active=True, original_base=float(context['base_amount']),
                   target_loss=money(-Decimal(str(state['sequence_pnl']))), last_event='ACTIVATED')
        return new
    if result == 'win' or seq_lost:
        state.update(sequence_pnl=0.0, sequence_known=True, sequence_signature=None)
    return state


def short_summary(doc):
    cfg = {**recovery_defaults(), **doc.get('recovery', {})}
    state = selected_state(doc)
    if cfg['mode'] == 'TRADITIONAL':
        return '🛟 التعزيز: تقليدي'
    if not state['active']:
        return f"🛟 التعزيز: جاهز | زيادة ${cfg['increment']:g}"
    effective, _ = sizing(doc)
    if cfg['exit_mode'] == 'WINS':
        progress = f"انتصارات {state['wins']}/{cfg['win_target']}"
    elif state['uncertain']:
        progress = 'نتائج غير مؤكدة'
    else:
        remaining = max(0, money(Decimal(str(state['target_loss']))-Decimal(str(state['phase_pnl']))))
        progress = f'متبقٍ ${remaining:.2f}'
    return f'🛟 تعزيز نشط: أساس ${effective:g} | {progress}'


def recovery_text(doc):
    cfg = {**recovery_defaults(), **doc.get('recovery', {})}
    state = selected_state(doc)
    effective, _ = sizing(doc)
    exit_label = 'تعويض صافي الخسارة' if cfg['exit_mode'] == 'RECOVER_LOSS' else f"{cfg['win_target']} انتصارات"
    lines = ['🛟 التعزيز بعد خسارة السلسلة',
             'الوضع: ' + ('تقليدي' if cfg['mode'] == 'TRADITIONAL' else 'مفعّل'),
             f"الأساس الأصلي: ${doc['trading']['trade_amount']:g} | الزيادة: +${cfg['increment']:g}",
             f'الرجوع للأساس: {exit_label}', short_summary(doc)]
    if state['active']:
        lines += [f"خسارة البداية: ${state['target_loss']:.2f} | صافي التعزيز: ${state['phase_pnl']:+.2f}",
                  f"أساس المرحلة: ${effective:g} | الانتصارات: {state['wins']}"]
    lines += ['الزيادة مرة واحدة؛ حدود الربح والخسارة لها الأولوية.',
              'تغيير إعداد التعزيز أو الأساس ينهي المرحلة الحالية دون تصفير الأرباح.']
    if state['active'] and state['uncertain']:
        lines += ['⚠️ نتيجة غير مؤكدة: لا يُعلن التعويض المالي تلقائياً؛ يمكنك الرجوع للتقليدي.']
    return '\n'.join(lines)
