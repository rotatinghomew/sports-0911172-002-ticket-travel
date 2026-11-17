from .store import require, money

class Alliance:
    def __init__(self, store):
        self.store = store

    def propose(self, id, receipts, shares):
        require(bool(receipts) and len(receipts)==len(set(receipts)), '对账单重复或为空')
        net = sum(self.store.get('receipt',key)['cents']-self.store.get('receipt',key)['refunded'] for key in receipts)
        for merchant, cents in shares.items():
            self.store.get('merchant',merchant)
            money(cents)
        require(sum(shares.values())==net,'人工分摊须与净额相等')
        return self.store.put('proposal',id,dict(receipts=receipts,shares=shares,
            net_cents=net,state='proposed'),create=True)

    def confirm(self, id, reviewer):
        with self.store.atomic():
            record = self.store.get('proposal',id)
            if record['state']=='confirmed':
                return record
            require(bool(reviewer),'确认人为空')
            net = sum(self.store.get('receipt',key)['cents']-self.store.get('receipt',key)['refunded'] for key in record['receipts'])
            require(net==record['net_cents'],'对账金额有变，请重新提议')
            record.update(state='confirmed',reviewer=reviewer)
            return self.store.put('proposal',id,record)
