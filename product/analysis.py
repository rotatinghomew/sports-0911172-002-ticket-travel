import hashlib
import itertools
import json
import math
from datetime import date, timedelta

from .store import require, money

CALIBER = 'increment-v1'

ASSUMPTION_LABELS = ('exclude_holidays', 'per_capita', 'adjust_baseline')
BASE_ASSUMPTIONS = dict(exclude_holidays=False, per_capita=False, adjust_baseline=True)

SCHEMES = ('by_effect', 'proportional_net', 'manual')

CORE_LIMITATIONS = [
    '节假日与季节因素仅通过前后对照部分校正，残余影响不可排除',
    '参加与未参加人群的原有出游差异通过活动前对照校正，校正依赖可比性假设',
    '同行消费包含同伴支出，人均口径仅作为敏感性假设呈现',
    '一人多票按旅程计一次，不重复计人；多人一单无法拆分到个人',
    '跨商户关联存在缺口，未关联消费不按人数或覆盖率外推',
    '许可关联不代表全体游客，结论仅适用于可支持范围',
    '窗口外因素（天气、同期其他活动、价格变化）未纳入',
]


def _day(value):
    return date.fromisoformat(str(value)[:10])


def _windows(activity):
    start = _day(activity['start'])
    end = _day(activity['end'])
    span = (end - start).days + 1
    pre_end = start - timedelta(days=1)
    pre_start = pre_end - timedelta(days=span - 1)
    holidays = {_day(item) for item in activity.get('holidays', [])}
    return pre_start, pre_end, start, end, holidays


def _slotter(activity, exclude_holidays):
    pre_start, pre_end, start, end, holidays = _windows(activity)

    def slot(day):
        if exclude_holidays and day in holidays:
            return None
        if pre_start <= day <= pre_end:
            return 'pre'
        if start <= day <= end:
            return 'post'
        return None

    return slot


def _round_cents(value):
    return math.floor(abs(value) + 0.5) * (1 if value >= 0 else -1)


def _apportion(raw, total):
    if not raw:
        return {}
    floors = {key: math.floor(value) for key, value in raw.items()}
    remainder = total - sum(floors.values())
    order = sorted(raw, key=lambda key: (-(raw[key] - floors[key]), key))
    keys = order if remainder > 0 else list(reversed(order))
    result = dict(floors)
    step = 1 if remainder > 0 else -1
    for key in itertools.islice(itertools.cycle(keys), abs(remainder)):
        result[key] += step
    return result


def _estimate(activity, view, journey_receipts, assumptions):
    slot = _slotter(activity, assumptions['exclude_holidays'])
    participant = set()
    for receipts in journey_receipts.values():
        participant.update(receipts)
    participant &= view.keys()
    cells = {}
    nets = {}
    for key, record in view.items():
        where = slot(record['day'])
        if where is None:
            continue
        group = 'participant' if key in participant else 'control'
        value = record['net'] / record['party_size'] if assumptions['per_capita'] else float(record['net'])
        cells.setdefault(record['merchant'], {}).setdefault(group, {}).setdefault(where, []).append(value)
        nets.setdefault(record['merchant'], {}).setdefault(group, {}).setdefault(where, []).append(record['net'])
    by_merchant = {}
    unexplainable = {}
    for merchant, groups in cells.items():
        p_post = groups.get('participant', {}).get('post', [])
        if not p_post:
            continue
        missing = []
        if not groups.get('participant', {}).get('pre'):
            missing.append('参加组活动前无关联消费')
        if not groups.get('control', {}).get('post'):
            missing.append('对照组活动期无消费')
        if not groups.get('control', {}).get('pre'):
            missing.append('对照组活动前无消费')
        if missing:
            unexplainable[merchant] = dict(
                linked_net=sum(nets[merchant]['participant']['post']), missing=missing)
            continue
        lift = sum(p_post) / len(p_post) - sum(groups['control']['post']) / len(groups['control']['post'])
        if assumptions['adjust_baseline']:
            lift -= sum(groups['participant']['pre']) / len(groups['participant']['pre']) \
                - sum(groups['control']['pre']) / len(groups['control']['pre'])
        by_merchant[merchant] = lift * len(p_post)
    return dict(total=sum(by_merchant.values()), by_merchant=by_merchant,
                unexplainable=unexplainable, cells=cells)


def _comparability(activity, view, journeys):
    slot = _slotter(activity, False)
    participant = set()
    for record in journeys.values():
        participant.update(record['receipts'])
    participant &= view.keys()
    control = [key for key in view if key not in participant]

    def keys(owner, when):
        source = participant if owner == 'participant' else control
        return [key for key in source if slot(view[key]['day']) == when]

    def mean_net(idlist):
        return round(sum(view[key]['net'] for key in idlist) / len(idlist), 2) if idlist else None

    def mean_party(idlist):
        return round(sum(view[key]['party_size'] for key in idlist) / len(idlist), 2) if idlist else None

    p_post, p_pre = keys('participant', 'post'), keys('participant', 'pre')
    c_post, c_pre = keys('control', 'post'), keys('control', 'pre')
    _, _, start, end, holidays = _windows(activity)
    return dict(
        participant=dict(journeys=len(journeys), orders_post=len(p_post), orders_pre=len(p_pre),
                         mean_post=mean_net(p_post), mean_pre=mean_net(p_pre),
                         party_mean_post=mean_party(p_post)),
        control=dict(orders_post=len(c_post), orders_pre=len(c_pre),
                     mean_post=mean_net(c_post), mean_pre=mean_net(c_pre),
                     party_mean_post=mean_party(c_post)),
        structures=dict(
            multi_ticket_journeys=sum(1 for record in journeys.values() if len(record['tickets']) > 1),
            multi_person_orders=sum(1 for key in p_post if view[key]['party_size'] > 1),
            holiday_days=sum(1 for day in holidays if start <= day <= end),
            holiday_orders=sum(1 for key in p_post if view[key]['day'] in holidays)),
        coverage=dict(links=len(journeys), linked_receipts=len(participant),
                      total_receipts=len(view),
                      linked_share=round(len(participant) / len(view), 4) if view else 0))


class Analysis:
    def __init__(self, store):
        self.store = store

    def activity(self, id, start, end, holidays=None, note=''):
        start_day = _day(start)
        end_day = _day(end)
        require(end_day >= start_day, '活动窗口无效')
        holiday_list = sorted({_day(item).isoformat() for item in holidays or []})
        return self.store.put('activity', id, dict(start=start_day.isoformat(),
            end=end_day.isoformat(), holidays=holiday_list, note=str(note)), create=True)

    def study(self, id, activity):
        activity_record = self.store.get('activity', activity)
        journeys = {record['id']: record for record in self.store.all('journey')
                    if record['consent'] == 'allowed'}
        receipts = {record['id']: record for record in self.store.all('receipt')}
        view = {key: dict(merchant=record['merchant'], net=record['cents'] - record['refunded'],
                          day=_day(record['happened_at']), party_size=record['party_size'])
                for key, record in receipts.items()}
        basis = dict(
            journeys={key: record['revision'] for key, record in sorted(journeys.items())},
            receipts={key: dict(revision=record['revision'], net=view[key]['net'])
                      for key, record in sorted(receipts.items())})
        digest = hashlib.sha256(
            json.dumps(basis, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        journey_receipts = {key: record['receipts'] for key, record in journeys.items()}
        runs = {}
        for flags in itertools.product((False, True), repeat=3):
            runs[flags] = _estimate(activity_record, view, journey_receipts,
                                    dict(zip(ASSUMPTION_LABELS, flags)))
        base_flags = tuple(BASE_ASSUMPTIONS[label] for label in ASSUMPTION_LABELS)
        base = runs[base_flags]
        comparability = _comparability(activity_record, view, journeys)
        limitations = list(CORE_LIMITATIONS)
        denied = sum(1 for record in self.store.all('journey') if record['consent'] != 'allowed')
        if denied:
            limitations.append(f'{denied} 段旅程未许可或已撤回，其消费按未参加处理')
        if comparability['structures']['holiday_days']:
            limitations.append(
                f"活动窗口包含 {comparability['structures']['holiday_days']} 天节假日，剔除口径见敏感性")
        for merchant, info in base['unexplainable'].items():
            limitations.append(
                f"商户 {merchant} {'、'.join(info['missing'])}，"
                f"活动期关联净额 {info['linked_net']} 分列入无法归属")
        coverage = comparability['coverage']
        limitations.append(
            f"许可关联 {coverage['links']} 段旅程、{coverage['linked_receipts']} 笔消费，"
            f"占全部 {coverage['total_receipts']} 笔消费的 {coverage['linked_share']}，未关联部分不外推")
        participant_post = sum(len(groups.get('participant', {}).get('post', ()))
                               for groups in base['cells'].values())
        control_post = sum(len(groups.get('control', {}).get('post', ()))
                           for groups in base['cells'].values())
        reasons = []
        if not journeys:
            reasons.append('当前没有许可关联的旅程')
        if not participant_post:
            reasons.append('许可关联在活动窗口内没有消费记录')
        if not control_post:
            reasons.append('活动窗口内缺少可比的未参加消费')
        if not base['by_merchant']:
            reasons.append('所有商户均缺少可比对照，无法界定增量')
        supportable = not reasons
        increment = None
        if supportable:
            total = _round_cents(base['total'])
            sensitivity = [dict(assumptions=dict(zip(ASSUMPTION_LABELS, flags)),
                                base=flags == base_flags,
                                total=_round_cents(run['total']))
                           for flags, run in sorted(runs.items())]
            totals = [item['total'] for item in sensitivity]
            increment = dict(assumptions=dict(BASE_ASSUMPTIONS), total=total,
                             by_merchant=_apportion(base['by_merchant'], total),
                             unexplainable=base['unexplainable'],
                             sensitivity=sensitivity, range=[min(totals), max(totals)])
        pre_start, pre_end, start, end, _ = _windows(activity_record)
        record = dict(activity=activity, caliber=CALIBER, basis=basis, basis_digest=digest,
                      change_sequence=self.store.sequence(),
                      windows=dict(pre_start=pre_start.isoformat(), pre_end=pre_end.isoformat(),
                                   start=start.isoformat(), end=end.isoformat(),
                                   holidays=list(activity_record['holidays'])),
                      comparability=comparability, limitations=limitations,
                      supportable=supportable, reasons=reasons, increment=increment)
        return self.store.put('study', id, record, create=True)

    def _proportional(self, study, total):
        activity = self.store.get('activity', study['activity'])
        slot = _slotter(activity, False)
        explainable = set(study['increment']['by_merchant'])
        weights = {}
        for key, pin in study['basis']['receipts'].items():
            receipt = self.store.get('receipt', key)
            merchant = receipt['merchant']
            if merchant not in explainable or slot(_day(receipt['happened_at'])) != 'post':
                continue
            weights[merchant] = weights.get(merchant, 0) + pin['net']
        if not any(weights.values()):
            weights = {merchant: 1 for merchant in explainable}
        whole = sum(weights.values())
        return _apportion({merchant: total * weight / whole for merchant, weight in weights.items()}, total)

    def schemes(self, study):
        record = self.store.get('study', study)
        require(record['supportable'], '研究缺少可支持范围，不能出具建议')
        increment = record['increment']
        total = increment['total']
        negative = {key: value for key, value in increment['by_merchant'].items() if value < 0}
        return dict(study=study, total=total, range=increment['range'],
                    assumptions=increment['assumptions'],
                    schemes=dict(by_effect=dict(shares=dict(increment['by_merchant'])),
                                 proportional_net=dict(shares=self._proportional(record, total))),
                    negative=negative, unexplainable=increment['unexplainable'],
                    note='各约定分摊同一份可解释增量；负向影响与无法归属部分不随约定消失')

    def recommend(self, id, study, scheme, shares=None):
        record = self.store.get('study', study)
        require(record['supportable'], '研究缺少可支持范围，不能出具建议')
        increment = record['increment']
        total = increment['total']
        require(total > 0, '可解释增量不为正，不出具分摊建议')
        require(scheme in SCHEMES, '未知经营约定')
        if scheme == 'by_effect':
            final = dict(increment['by_merchant'])
        elif scheme == 'proportional_net':
            final = self._proportional(record, total)
        else:
            require(isinstance(shares, dict) and bool(shares), '人工约定须给出分摊')
            for merchant, cents in shares.items():
                self.store.get('merchant', merchant)
                money(cents)
            final = dict(shares)
        require(sum(final.values()) == total, '分摊须与可解释增量相等')
        negative = {key: value for key, value in increment['by_merchant'].items() if value < 0}
        return self.store.put('recommendation', id, dict(
            study=study, study_revision=record['revision'], caliber=record['caliber'],
            scheme=scheme, assumptions=increment['assumptions'], total=total, shares=final,
            negative=negative, unexplainable=increment['unexplainable'],
            range=increment['range'], basis_digest=record['basis_digest'],
            state='proposed'), create=True)

    def confirm(self, id, reviewer):
        with self.store.atomic():
            record = self.store.get('recommendation', id)
            if record['state'] == 'confirmed':
                return record
            require(bool(reviewer), '确认人为空')
            study = self.store.get('study', record['study'])
            for journey_id, revision in study['basis']['journeys'].items():
                journey = self.store.get('journey', journey_id)
                require(journey['consent'] == 'allowed' and journey['revision'] == revision,
                        '分析依据的关联许可已变化，需重新研究')
            for receipt_id, pin in study['basis']['receipts'].items():
                require(self.store.get('receipt', receipt_id)['revision'] == pin['revision'],
                        '对账依据已变化，需重新研究')
            record.update(state='confirmed', reviewer=reviewer)
            return self.store.put('recommendation', id, record)

    def _view_from_basis(self, basis, use_current_net):
        view = {}
        for key, pin in basis['receipts'].items():
            receipt = self.store.get('receipt', key)
            net = receipt['cents'] - receipt['refunded'] if use_current_net else pin['net']
            view[key] = dict(merchant=receipt['merchant'], net=net,
                             day=_day(receipt['happened_at']), party_size=receipt['party_size'])
        return view

    def reconcile(self, id):
        record = self.store.get('recommendation', id)
        study = self.store.get('study', record['study'])
        activity = self.store.get('activity', study['activity'])
        basis = study['basis']
        journeys_a = {key: self.store.get('journey', key)['receipts'] for key in basis['journeys']}
        run_a = _estimate(activity, self._view_from_basis(basis, False), journeys_a,
                          dict(BASE_ASSUMPTIONS))
        view_b = self._view_from_basis(basis, True)
        run_b = _estimate(activity, view_b, journeys_a, dict(BASE_ASSUMPTIONS))
        journeys_c = {}
        for key in basis['journeys']:
            journey = self.store.get('journey', key)
            if journey['consent'] == 'allowed':
                journeys_c[key] = journey['receipts']
        run_c = _estimate(activity, view_b, journeys_c, dict(BASE_ASSUMPTIONS))
        changes = self.store.changes_after(study['change_sequence'])
        new_facts = dict(
            new_receipts=len([item for item in changes
                              if item['kind'] == 'receipt' and item['revision'] == 1]),
            updated_basis_receipts=len({item['key'] for item in changes
                                        if item['kind'] == 'receipt' and item['revision'] > 1
                                        and item['key'] in basis['receipts']}),
            new_journeys=len([item for item in changes
                              if item['kind'] == 'journey' and item['revision'] == 1]),
            revoked_basis_journeys=len({item['key'] for item in changes
                                        if item['kind'] == 'journey' and item['revision'] > 1
                                        and item['key'] in basis['journeys']}),
            new_refunds=len([item for item in changes if item['kind'] == 'refund']))
        return dict(recommendation=id, state=record['state'], issued_total=record['total'],
                    caliber=dict(study=study['caliber'], current=CALIBER),
                    delta=dict(caliber=_round_cents(run_a['total']) - study['increment']['total'],
                               refund=_round_cents(run_b['total']) - _round_cents(run_a['total']),
                               coverage=_round_cents(run_c['total']) - _round_cents(run_b['total'])),
                    current_equivalent=_round_cents(run_c['total']), new_facts=new_facts,
                    note='已出具建议保持不变；差异按口径、退款、关联覆盖分开核对，新增事实另起研究')

    def explain(self, id):
        record = self.store.get('recommendation', id)
        study = self.store.get('study', record['study'])
        return dict(recommendation=id, state=record['state'], reviewer=record.get('reviewer'),
                    scheme=record['scheme'], caliber=record['caliber'],
                    assumptions=record['assumptions'], total=record['total'], range=record['range'],
                    shares=[dict(merchant=merchant, cents=cents)
                            for merchant, cents in sorted(record['shares'].items())],
                    negative=record['negative'], unexplainable=record['unexplainable'],
                    comparability=study['comparability'], limitations=study['limitations'],
                    study=record['study'], basis_digest=record['basis_digest'],
                    note='金额为依据研究创建时许可关联与对账净额的估计，范围为假设敏感性；本说明不含个人消费路径')
