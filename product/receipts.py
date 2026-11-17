from .store import require, money

class Receipts:
    def __init__(self, store):
        self.store = store

    def merchant(self, id, city, sector):
        return self.store.put('merchant',id,dict(city=city,sector=sector),create=True)

    def post(self, id, merchant, cents, happened_at, party_size=1, source='walkin'):
        self.store.get('merchant',merchant)
        require(party_size>0, '同行人数无效')
        return self.store.put('receipt',id,dict(merchant=merchant,cents=money(cents),
            refunded=0,happened_at=happened_at,party_size=party_size,source=source),create=True)

    def refund(self, id, receipt, cents):
        with self.store.atomic():
            record = self.store.get('receipt',receipt)
            require(record['refunded']+money(cents)<=record['cents'],'退款超额')
            result = self.store.put('refund',id,dict(receipt=receipt,cents=cents),create=True)
            record['refunded']+=cents
            self.store.put('receipt',receipt,record)
            return result

    def statement(self, merchant):
        records = [item for item in self.store.all('receipt') if item['merchant']==merchant]
        return dict(merchant=merchant,net_cents=sum(item['cents']-item['refunded'] for item in records))
