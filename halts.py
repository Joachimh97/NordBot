"""Independent, durable entry latches. A manual reset never clears automatic risk."""
import time

PRIORITY = {'total': 0, 'daily': 1, 'rolling': 2, 'streak': 3,
            'protection': 4, 'integrity': 5, 'manual': 20}


def legacy_id(reason):
    if reason == 'Manuell nødstopp': return 'manual'
    if reason == 'Samlet tapsgrense': return 'total'
    if reason == 'Døgnets tapsgrense': return 'daily'
    if reason.startswith('Rullende'): return 'rolling'
    if 'tapende handler på rad' in reason: return 'streak'
    if reason.startswith('Beskyttelsesordre'): return 'protection'
    return 'legacy'


def display(state):
    active = state.get('halts', {})
    if not active: return ''
    first = min(active, key=lambda k: (PRIORITY.get(k, 10), active[k]['created']))
    return active[first]['reason']


def initialize(state, now=None):
    """One-way migration; preserve an old/unknown reason rather than drop it."""
    now = time.time() if now is None else now
    if 'halts' not in state:
        state['halts'] = {}
        if state.get('halt'):
            reason = state['halt']; key = legacy_id(reason)
            state['halts'][key] = {'reason': reason, 'created': now,
                                   'release': 'utc_day' if key == 'daily' else 'review',
                                   'day': state.get('day', '')}
    # Old code / an imported snapshot may contain an additional legacy latch.
    old = state.get('halt', '')
    if old and old != display(state) and not any(x['reason'] == old for x in state['halts'].values()):
        state['halts'][legacy_id(old)] = {'reason': old, 'created': now, 'release': 'review',
                                        'day': state.get('day', '')}
    state['halt'] = display(state)
    return state['halts']


def latch(state, key, reason, now=None):
    now = time.time() if now is None else now
    active = initialize(state, now)
    if key not in active:
        active[key] = {'reason': reason, 'created': now,
                       'release': 'manual' if key == 'manual' else ('utc_day' if key == 'daily' else 'review'),
                       'day': state.get('day', '')}
    state['halt'] = display(state)


def release_manual(state):
    active = initialize(state)
    if 'manual' not in active: return False
    del active['manual']
    state['halt'] = display(state)
    return True


def rollover(state, day):
    active = initialize(state)
    daily = active.get('daily')
    if daily and daily.get('day') != day:
        del active['daily']
    state['halt'] = display(state)


def reasons(state):
    return [v['reason'] for _, v in sorted(initialize(state).items(),
            key=lambda item: (PRIORITY.get(item[0], 10), item[1]['created']))]
