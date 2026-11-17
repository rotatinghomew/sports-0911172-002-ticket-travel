from .store import require, money

class Journeys:
    def __init__(self, store):
        self.store = store

    def link(self, id, tickets, receipts, consent, household):
        require(consent in ('allowed','denied'), '许可无效')
        require(len(tickets)==len(set(tickets)) and bool(tickets), '票根无效')
        for receipt in receipts:
            self.store.get('receipt',receipt)
        return self.store.put('journey',id,dict(tickets=tickets,receipts=receipts,
            consent=consent,household=household),create=True)

    def revoke(self, id, happened_at):
        record = self.store.get('journey',id)
        record.update(consent='denied',revoked_at=happened_at)
        return self.store.put('journey',id,record)

    def permitted(self):
        return [record for record in self.store.all('journey') if record['consent']=='allowed']

    def coverage(self):
        records = self.permitted()
        receipts = set(item for record in records for item in record['receipts'])
        return dict(links=len(records),receipts=len(receipts))
