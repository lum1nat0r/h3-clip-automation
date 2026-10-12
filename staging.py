"""Separate encoding, sampling and decoding with installed Inspire nodes.

Only conditioning and the empty AV latent cross the boundary. Model objects
stay in the first graph's execution cache, which /free resets between stages.
"""
import copy
import re
import urllib.parse

CACHE_NODE = 'CacheBackendData //Inspire'
LOAD_NODE = 'RetrieveBackendData //Inspire'


def cache_keys(token, phase='encoding'):
    if not re.fullmatch(r'[a-f0-9]{32}', token):
        raise ValueError('Invalid private cache token')
    kinds = ('conditioning', 'latent') if phase == 'encoding' else ('sampled', 'resolved', 'pins')
    return [f'h3-auto-{token}-{kind}' for kind in kinds]


def split_graph(graph, token):
    keys = cache_keys(token)
    # Keep exactly the dependency closure needed for reference/text encoding.
    keep = set()
    def visit(key):
        if key in keep:
            return
        keep.add(key)
        for value in graph[key]['inputs'].values():
            if isinstance(value, list):
                visit(str(value[0]))
    visit('9')
    encode = {k: copy.deepcopy(v) for k, v in graph.items() if k in keep}
    for slot, key in enumerate(keys):
        encode[str(90 + slot)] = {'class_type': CACHE_NODE, 'inputs': {
            'key': key, 'tag': 'h3-auto-' + ('conditioning' if slot == 0 else 'latent'),
            'data': ['9', slot]}}
    sample = copy.deepcopy(graph)
    sample.pop('5')  # Never load the text encoder during video generation.
    sample['9'] = {'class_type': LOAD_NODE, 'inputs': {'key': keys[0]}}
    sample['89'] = {'class_type': LOAD_NODE, 'inputs': {'key': keys[1]}}
    for node in sample.values():
        for name, value in node['inputs'].items():
            if value == ['9', 1]:
                node['inputs'][name] = ['89', 0]
    # Decoding can have a substantial peak too; release diffusion weights first.
    decoded_keys = cache_keys(token, 'sampling')
    for n in ('17', '18', '19', '20'):
        sample.pop(n)
    for slot, (wire, key) in enumerate(zip((['16',1], ['11',0], ['11',2]), decoded_keys)):
        sample[str(92 + slot)] = {'class_type': CACHE_NODE, 'inputs': {
            'key': key, 'tag': 'h3-auto-' + ('sampled','resolved','pins')[slot], 'data': wire}}
    decode = {k: copy.deepcopy(graph[k]) for k in ('6','7','17','18','19','20')}
    for slot, key in enumerate(decoded_keys):
        decode[str(96 + slot)] = {'class_type': LOAD_NODE, 'inputs': {'key': key}}
    for node in decode.values():
        for name, value in node['inputs'].items():
            if value == ['16',1]: node['inputs'][name] = ['96',0]
            elif value == ['11',0]: node['inputs'][name] = ['97',0]
            elif value == ['11',2]: node['inputs'][name] = ['98',0]
    return encode, sample, decode


def listed_keys(value):
    if not isinstance(value, str) or '---- [String Key Caches] ----' not in value:
        raise ValueError('Inspire-Cache-Liste ist nicht lesbar.')
    section = value.split('---- [String Key Caches] ----', 1)[1].split('---- [Number Key Caches] ----', 1)[0]
    return {line.partition(':')[0].strip() for line in section.splitlines() if ':' in line}


def cache_present(call, base, token, phase='encoding'):
    return set(cache_keys(token, phase)) <= listed_keys(call(base, '/inspire/cache/list', timeout=8))


def clear_cache(call, base, token):
    existing = listed_keys(call(base, '/inspire/cache/list', timeout=8))
    for key in cache_keys(token) + cache_keys(token, 'sampling'):
        if key in existing:
            call(base, '/inspire/cache/remove?' + urllib.parse.urlencode({'key': key}), timeout=8)
