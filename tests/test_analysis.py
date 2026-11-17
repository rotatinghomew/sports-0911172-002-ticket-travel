import json
import unittest
from pathlib import Path
from product.app import Application

DATA = Path(__file__).parent.parent / 'data' / 'increment.json'


class IncrementAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        self.app.replay(json.loads(DATA.read_text()))

    def tearDown(self):
        self.app.store.close()

    def study(self, key):
        return self.app.store.get('study', key)

    def test_study_pins_legal_basis_at_creation(self):
        s1 = self.study('s1')
        self.assertEqual(sorted(s1['basis']['journeys']), ['j1', 'j2', 'j3', 'j4'])
        s2 = self.study('s2')
        self.assertEqual(sorted(s2['basis']['journeys']), ['j1', 'j2', 'j3'])
        self.assertNotIn('j9', s2['basis']['journeys'])
        rec2 = self.app.store.get('recommendation', 'rec2')
        self.assertEqual(rec2['basis_digest'], s2['basis_digest'])
        self.assertEqual(rec2['study_revision'], s2['revision'])

    def test_comparability_keeps_structure_limits(self):
        comparability = self.study('s2')['comparability']
        self.assertEqual(comparability['structures']['multi_ticket_journeys'], 1)
        self.assertEqual(comparability['structures']['multi_person_orders'], 4)
        self.assertEqual(comparability['structures']['holiday_days'], 2)
        self.assertEqual(comparability['coverage']['links'], 3)
        self.assertEqual(comparability['coverage']['linked_receipts'], 11)
        self.assertEqual(comparability['coverage']['total_receipts'], 25)
        text = json.dumps(self.study('s2')['limitations'], ensure_ascii=False)
        self.assertIn('不代表全体游客', text)
        self.assertIn('不按人数或覆盖率外推', text)

    def test_increment_is_explainable_and_allocated_once(self):
        increment = self.study('s2')['increment']
        self.assertEqual(increment['total'], 17000)
        self.assertEqual(increment['by_merchant'], {'hotel': 14000, 'diner': 7000, 'cafe': -4000})
        self.assertEqual(increment['unexplainable']['shop']['linked_net'], 20000)
        rec2 = self.app.store.get('recommendation', 'rec2')
        self.assertEqual(sum(rec2['shares'].values()), rec2['total'])
        self.assertEqual(rec2['negative'], {'cafe': -4000})
        self.assertEqual(rec2['unexplainable']['shop']['linked_net'], 20000)

    def test_sensitivity_range_not_single_amount(self):
        increment = self.study('s2')['increment']
        self.assertEqual(len(increment['sensitivity']), 8)
        base = [item for item in increment['sensitivity'] if item['base']]
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0]['total'], 17000)
        self.assertEqual(increment['range'], [-50500, 23000])
        totals = {tuple(item['assumptions'][label] for label in
                        ('exclude_holidays', 'per_capita', 'adjust_baseline')): item['total']
                  for item in increment['sensitivity']}
        self.assertEqual(totals[(False, False, False)], 23000)
        self.assertEqual(totals[(False, True, True)], -50500)

    def test_schemes_share_same_increment(self):
        view = self.app.execute('analysis.schemes', dict(study='s2'))
        by_effect = view['schemes']['by_effect']['shares']
        proportional = view['schemes']['proportional_net']['shares']
        self.assertEqual(sum(by_effect.values()), 17000)
        self.assertEqual(sum(proportional.values()), 17000)
        self.assertEqual(proportional, {'hotel': 11887, 'diner': 4090, 'cafe': 1023})
        self.assertNotEqual(by_effect, proportional)
        self.assertEqual(view['negative'], {'cafe': -4000})
        self.assertEqual(view['unexplainable']['shop']['linked_net'], 20000)

    def test_revocation_blocks_unconfirmed_recommendation(self):
        rec1 = self.app.store.get('recommendation', 'rec1')
        self.assertEqual(rec1['state'], 'proposed')
        self.assertEqual(rec1['total'], 26000)
        with self.assertRaises(ValueError):
            self.app.execute('analysis.confirm', dict(id='rec1', reviewer='finance'))

    def test_confirmed_statement_not_rewritten_and_reconcile_splits_causes(self):
        before = self.app.store.get('recommendation', 'rec2')
        result = self.app.execute('analysis.reconcile', dict(id='rec2'))
        self.assertEqual(result['issued_total'], 17000)
        self.assertEqual(result['delta'], {'caliber': 0, 'refund': -5000, 'coverage': -12083})
        self.assertEqual(result['current_equivalent'], -83)
        self.assertEqual(result['new_facts']['new_refunds'], 1)
        self.assertEqual(result['new_facts']['revoked_basis_journeys'], 1)
        self.assertEqual(result['new_facts']['updated_basis_receipts'], 1)
        after = self.app.store.get('recommendation', 'rec2')
        self.assertEqual(after, before)

    def test_explain_hides_personal_paths(self):
        view = self.app.execute('analysis.explain', dict(id='rec2'))
        text = json.dumps(view, ensure_ascii=False)
        for token in ('j1', 'j2', 'j3', 't1', 'hh1', 'p_h_post', 'x_d_post'):
            self.assertNotIn(token, text)
        self.assertEqual(view['total'], 17000)
        self.assertEqual(view['range'], [-50500, 23000])
        self.assertTrue(view['limitations'])
        self.assertIn('shop', text)
        self.assertEqual(view['basis_digest'], self.study('s2')['basis_digest'])

    def test_unsupportable_study_shows_reasons_not_amount(self):
        app = Application()
        try:
            app.execute('receipts.merchant', dict(id='m', city='c', sector='s'))
            app.execute('receipts.post', dict(id='r', merchant='m', cents=1000,
                                              happened_at='2026-10-16'))
            app.execute('analysis.activity', dict(id='a', start='2026-10-15', end='2026-10-21'))
            study = app.execute('analysis.study', dict(id='s', activity='a'))
            self.assertFalse(study['supportable'])
            self.assertIsNone(study['increment'])
            self.assertTrue(study['reasons'])
            with self.assertRaises(ValueError):
                app.execute('analysis.recommend', dict(id='x', study='s', scheme='by_effect'))
            with self.assertRaises(ValueError):
                app.execute('analysis.schemes', dict(study='s'))
        finally:
            app.store.close()

    def test_manual_scheme_must_equal_total(self):
        with self.assertRaises(ValueError):
            self.app.execute('analysis.recommend', dict(id='rec3', study='s2', scheme='manual',
                                                        shares={'hotel': 17001}))
        rec3 = self.app.execute('analysis.recommend', dict(id='rec3', study='s2', scheme='manual',
                                                           shares={'hotel': 10000, 'diner': 7000}))
        self.assertEqual(rec3['shares'], {'hotel': 10000, 'diner': 7000})

    def test_refund_after_study_blocks_confirm(self):
        app = Application()
        try:
            app.execute('receipts.merchant', dict(id='m', city='c', sector='s'))
            app.execute('receipts.post', dict(id='c_pre', merchant='m', cents=1000,
                                              happened_at='2026-10-09'))
            app.execute('receipts.post', dict(id='c_post', merchant='m', cents=1000,
                                              happened_at='2026-10-16'))
            app.execute('receipts.post', dict(id='p_pre', merchant='m', cents=1000,
                                              happened_at='2026-10-10'))
            app.execute('receipts.post', dict(id='p_post', merchant='m', cents=3000,
                                              happened_at='2026-10-16'))
            app.execute('journeys.link', dict(id='j', tickets=['t'], receipts=['p_pre', 'p_post'],
                                              consent='allowed', household='h'))
            app.execute('analysis.activity', dict(id='a', start='2026-10-15', end='2026-10-21'))
            study = app.execute('analysis.study', dict(id='s', activity='a'))
            self.assertEqual(study['increment']['total'], 2000)
            app.execute('analysis.recommend', dict(id='rec', study='s', scheme='by_effect'))
            app.execute('receipts.refund', dict(id='rf', receipt='p_post', cents=500))
            with self.assertRaises(ValueError):
                app.execute('analysis.confirm', dict(id='rec', reviewer='finance'))
        finally:
            app.store.close()

    def test_confirm_is_idempotent(self):
        again = self.app.execute('analysis.confirm', dict(id='rec2', reviewer='finance'))
        self.assertEqual(again['state'], 'confirmed')
        self.assertEqual(again['total'], 17000)


if __name__ == '__main__':
    unittest.main()
