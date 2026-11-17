import json
import unittest
from pathlib import Path
from product.app import Application


class BusinessBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.app=Application()
        self.app.replay(json.loads((Path(__file__).parent.parent/'data'/'demo.json').read_text()))

    def tearDown(self):
        self.app.store.close()
    def test_consent_revocation_keeps_financial_fact(self):
        self.app.execute('journeys.revoke',dict(id='j1',happened_at='2026-10-19'))
        self.assertEqual(self.app.execute('journeys.coverage',{}),dict(links=0,receipts=0))
        self.assertEqual(self.app.execute('receipts.statement',dict(merchant='hotel'))['net_cents'],30000)

    def test_stale_manual_proposal_cannot_be_confirmed(self):
        self.app.execute('alliance.propose',dict(id='p2',receipts=['h1'],shares={'hotel':30000}))
        self.app.execute('receipts.refund',dict(id='r2',receipt='h1',cents=5000))
        with self.assertRaises(ValueError):
            self.app.execute('alliance.confirm',dict(id='p2',reviewer='finance'))
