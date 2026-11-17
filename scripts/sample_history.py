import random
import json
from datetime import date, timedelta

def generate():
    rng = random.Random(117202)
    operations = []

    def add(command, **arguments):
        operations.append([command, arguments])

    def day(offset):
        return (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
    for index in range(12):
        add('receipts.merchant', id=f'm{index}', city=f'city{index % 3}', sector=['hotel', 'food', 'retail'][index % 3])
    for index in range(320):
        household = f'h{index % 100}'
        receipts = []
        for part in range(1 + index % 3):
            key = f'r{index}:{part}'
            receipts.append(key)
            amount = rng.choice([4000, 8000, 20000, 40000])
            add('receipts.post', id=key, merchant=f'm{(index + part) % 12}', cents=amount, happened_at=day(index % 84), party_size=1 + index % 4, source='ticket' if index % 3 else 'walkin')
            if rng.random() < 0.08:
                add('receipts.refund', id=f'f{key}', receipt=key, cents=amount // 2)
        if index % 5:
            add('journeys.link', id=f'j{index}', tickets=[f't{index}', f't{index}b'] if index % 4 == 0 else [f't{index}'], receipts=receipts, consent='denied' if index % 9 == 0 else 'allowed', household=household)
            if index % 17 == 0:
                add('journeys.revoke', id=f'j{index}', happened_at=day(index % 84 + 2))
    return operations

def main():
    import argparse
    parser = argparse.ArgumentParser(description='生成本地合成业务记录，不包含真实个人资料')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from pathlib import Path
    Path(args.output).write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + '\n')

if __name__=='__main__':
    main()
