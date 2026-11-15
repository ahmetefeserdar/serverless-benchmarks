# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# Browser-agent tasks of increasing size. Each task is a fixed sequence of browser actions,
# as an agent issues them through Playwright MCP, on a stored website snapshot:
#   test:  fill a one-field form (static HTML, no external resources)
#   small: read a Wikipedia article and jump to a section (real page, pinned revision
#          1375122930 of "Serverless computing", CC BY-SA, with its styles and images)
#   large: buy the cheapest pair of headphones in a client-side rendered shop with 2000
#          products (modeled on the shopping site of WebArena; catalog generated below)

import json
import os
import random
import shutil
import tarfile
import tempfile

SITES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sites')

# Default context budget of Playwright MCP and the MCP fetch tool.
MAX_LENGTH = 5000

SHOP_PRODUCTS = 2000
SHOP_SEED = 42
SHOP_QUERY = 'headphones'
SHOP_QUANTITY = 2

CATEGORIES = {
    'Audio': ['Headphones', 'Earbuds', 'Speaker', 'Soundbar', 'Turntable'],
    'Computers': ['Laptop', 'Monitor', 'Keyboard', 'Mouse', 'Webcam'],
    'Phones': ['Smartphone', 'Phone Case', 'Charger', 'Power Bank'],
    'Home': ['Lamp', 'Vacuum Cleaner', 'Air Purifier', 'Thermostat'],
    'Kitchen': ['Coffee Maker', 'Blender', 'Kettle', 'Toaster', 'Knife Set'],
    'Sports': ['Running Shoes', 'Yoga Mat', 'Bicycle Helmet', 'Water Bottle'],
}
BRANDS = ['Acme', 'Nordwind', 'Helvetica', 'Zenith', 'Polaris', 'Kestrel', 'Lumen', 'Orbit']
ADJECTIVES = ['Wireless', 'Compact', 'Pro', 'Classic', 'Ultra', 'Eco', 'Smart', 'Travel']
REVIEWERS = ['Anna', 'Luca', 'Mei', 'Noah', 'Sofia', 'Jonas', 'Lea', 'Omar']
REVIEW_TEXTS = ['Works as described.', 'Great value for the price.', 'Stopped working after a month.',
                'Arrived quickly and well packaged.', 'Better than my previous one.', 'A bit too expensive.']


def shop_catalog():
    rng = random.Random(SHOP_SEED)
    products = []
    for i in range(SHOP_PRODUCTS):
        category = rng.choice(sorted(CATEGORIES))
        name = f'{rng.choice(BRANDS)} {rng.choice(ADJECTIVES)} {rng.choice(CATEGORIES[category])}'
        products.append({
            'id': i + 1,
            'name': name,
            'category': category,
            'price_cents': rng.randint(499, 199999),
            'rating': round(rng.uniform(1.0, 5.0), 1),
            'description': f'The {name} from our {category.lower()} range. Free shipping and 30-day returns.',
            'reviews': [{'author': rng.choice(REVIEWERS), 'stars': rng.randint(1, 5), 'text': rng.choice(REVIEW_TEXTS)}
                        for _ in range(3)],
        })
    return {'categories': sorted(CATEGORIES), 'products': products}


def shop_expected(catalog):
    # Same search and order as the shop's "Price: low to high".
    found = [p for p in catalog['products'] if SHOP_QUERY in p['name'].lower()]
    cheapest = min(found, key=lambda p: (p['price_cents'], p['id']))
    total = cheapest['price_cents'] * SHOP_QUANTITY
    return ['Order confirmed', f"{SHOP_QUANTITY} × {cheapest['name']}", f'Total: CHF {total / 100:.2f}']


TASKS = {
    'test': {
        'site': 'form',
        'actions': [
            {'op': 'navigate', 'path': 'index.html'},
            {'op': 'type', 'selector': '#name', 'text': 'SeBS'},
            {'op': 'click', 'selector': '#greet'},
            {'op': 'wait_for', 'text': 'Hello, SeBS!'},
            {'op': 'snapshot'},
        ],
        'expect': ['Hello, SeBS!'],
    },
    'small': {
        'site': 'wiki',
        'actions': [
            {'op': 'navigate', 'path': 'index.html'},
            {'op': 'snapshot'},
            {'op': 'click', 'selector': 'a[href="#Challenges"]'},
            {'op': 'extract_text', 'selector': '#mw-content-text'},
            {'op': 'screenshot'},
        ],
        'expect': ['Serverless computing', 'Vendor lock-in'],
    },
    'large': {
        'site': 'shop',
        'actions': [
            {'op': 'navigate', 'path': 'index.html'},
            {'op': 'wait_for', 'text': f'{SHOP_PRODUCTS} products'},
            {'op': 'type', 'selector': '#search', 'text': SHOP_QUERY},
            {'op': 'select', 'selector': '#sort', 'value': 'price-asc'},
            {'op': 'click', 'selector': '#search-btn'},
            {'op': 'snapshot'},
            {'op': 'click', 'selector': '#results .product-card:first-child a.view'},
            {'op': 'type', 'selector': '#qty', 'text': str(SHOP_QUANTITY)},
            {'op': 'click', 'selector': '#add-to-cart'},
            {'op': 'wait_for', 'text': f'Added {SHOP_QUANTITY} to cart.'},
            {'op': 'click', 'selector': '#cart-link'},
            {'op': 'click', 'selector': '#checkout'},
            {'op': 'type', 'selector': '#name', 'text': 'Ada Lovelace'},
            {'op': 'type', 'selector': '#email', 'text': 'ada@example.com'},
            {'op': 'type', 'selector': '#address', 'text': 'Universitaetstrasse 6, 8092 Zurich'},
            {'op': 'click', 'selector': '#place-order'},
            {'op': 'wait_for', 'text': 'Order confirmed'},
            {'op': 'snapshot'},
            {'op': 'screenshot', 'full_page': True},
        ],
        'expect': None,  # computed from the generated catalog
    },
}


def buckets_count():
    return (1, 0)


def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):
    task = TASKS[size]
    expect = task['expect']
    with tempfile.TemporaryDirectory() as tmp:
        site_dir = os.path.join(tmp, task['site'])
        shutil.copytree(os.path.join(SITES_DIR, task['site']), site_dir)
        if task['site'] == 'shop':
            catalog = shop_catalog()
            with open(os.path.join(site_dir, 'catalog.json'), 'w') as f:
                json.dump(catalog, f)
            expect = shop_expected(catalog)
        archive = os.path.join(tmp, f"{task['site']}.tar.gz")
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(site_dir, arcname='.')
        upload_func(0, f"{task['site']}.tar.gz", archive)

    return {
        'bucket': {'bucket': benchmarks_bucket, 'input': input_paths[0]},
        'site': f"{task['site']}.tar.gz",
        'actions': task['actions'],
        'max_length': MAX_LENGTH,
        'reuse_browser': False,
        'size': size,
        'expect': expect,
    }


def validate_output(data_dir: str | None, input_config: dict, output: dict, language: str, storage=None) -> str | None:
    result = output.get('result', {})
    steps = result.get('steps', [])
    actions = input_config['actions']

    for i, step in enumerate(steps):
        if not step.get('ok'):
            return f"Action {i} ({step.get('op')}) failed: {step.get('error')}"
    if len(steps) != len(actions):
        return f"Performed {len(steps)} actions, the task has {len(actions)}"

    content = result.get('content', '')
    for text in input_config['expect']:
        if text not in content:
            return f"Expected '{text}' in the page content the agent reads"

    if len(content) > input_config['max_length'] + 200:
        return f"Content not truncated: {len(content)} chars > max_length {input_config['max_length']}"

    expected_screenshots = sum(1 for a in actions if a['op'] == 'screenshot')
    screenshots = result.get('screenshots', [])
    if len(screenshots) != expected_screenshots or any(s.get('screenshot_bytes', 0) < 1000 for s in screenshots):
        return f"Expected {expected_screenshots} non-empty screenshots, got {screenshots}"

    return None
