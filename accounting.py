"""Canonical decimal ledger; floating-point fields are display-only projections."""
from decimal import Decimal, localcontext

FIELDS = ('cash', 'qty', 'basis', 'fees', 'realized', 'position_realized',
          'entry_volume', 'entry_cost')


def dec(value):
    value = Decimal(str(value))
    if not value.is_finite(): raise ValueError('Ugyldig desimaltall i regnskapet.')
    return value


class Ledger:
    def __init__(self, state):
        self.state = state
        state.setdefault('money', {})
        for field in FIELDS:
            state['money'].setdefault(field, str(dec(state.get(field, 0))))
        self.project()

    def get(self, key): return dec(self.state['money'][key])

    def set(self, **values):
        for key, value in values.items():
            self.state['money'][key] = format(dec(value), 'f')
        self.project()

    def project(self):
        for key, value in self.state['money'].items(): self.state[key] = float(dec(value))

    def buy(self, quantity, cost, fee, first=False):
        q, c, f = map(dec, (quantity, cost, fee))
        if q < 0 or c < 0 or (q > 0 and c <= 0): raise ValueError('Ugyldig kjøpsutførelse.')
        if first: self.set(entry_volume=0, entry_cost=0, position_realized=0)
        with localcontext() as ctx:
            ctx.prec = 40
            self.set(cash=self.get('cash')-c-f, qty=self.get('qty')+q,
                     basis=self.get('basis')+c+f, fees=self.get('fees')+f,
                     entry_volume=self.get('entry_volume')+q,
                     entry_cost=self.get('entry_cost')+c)

    def sell(self, quantity, cost, fee, tolerance='0'):
        q, c, f = map(dec, (quantity, cost, fee))
        owned = self.get('qty')
        if q < 0 or c < 0 or q > owned + dec(tolerance): raise ValueError('Salg overstiger botens beholdning.')
        with localcontext() as ctx:
            ctx.prec = 40
            used = self.get('basis') * min(Decimal(1), q/owned) if owned else Decimal(0)
            rest = max(Decimal(0), owned-q)
            pnl = c-f-used
            self.set(cash=self.get('cash')+c-f, qty=rest,
                     basis=self.get('basis')-used, fees=self.get('fees')+f,
                     realized=self.get('realized')+pnl,
                     position_realized=self.get('position_realized')+pnl)
            return pnl

    def equity(self, bid, fee, slippage):
        with localcontext() as ctx:
            ctx.prec = 40
            return self.get('cash')+self.get('qty')*dec(bid)*(1-dec(fee))*(1-dec(slippage))
