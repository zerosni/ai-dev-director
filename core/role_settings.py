"""Validated, atomic next-run role preferences (separate from base YAML)."""
import json
import os
import threading
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / 'data' / 'role_settings.json'
LOCK = threading.Lock()
ALIASES = {'codex': 'openai', 'openai': 'openai', 'claude': 'claude',
           'anthropic': 'claude', 'google': 'antigravity', 'antigravity': 'antigravity',
           'local': 'local'}   # local LLM: config.yaml only, no cloud domain (not in DOMAINS)
DOMAINS = {'openai': 'OPENAI', 'claude': 'ANTHROPIC', 'antigravity': 'GOOGLE'}

def validate(roles):
    if not isinstance(roles, dict) or set(roles) != {'head', 'builder', 'reviewer'}:
        raise ValueError('HEAD, BUILDER, REVIEWER 세 역할을 모두 지정해야 합니다.')
    if any(v not in DOMAINS for v in roles.values()) or len(set(roles.values())) != 3:
        raise ValueError('세 역할에는 서로 다른 AI를 지정해야 합니다.')
    return dict(roles)

def apply(config):
    if PATH.exists():
        roles = validate(json.loads(PATH.read_text(encoding='utf-8')))
        for role, provider in roles.items():
            config.setdefault(role, {}).update(provider=provider, executable=None, model='cli-default')
        config['builder']['mode'] = 'proposal'
        config['manual_roles'] = True
    return config

def current(config):
    return {role: ALIASES[config[role]['provider'].lower()] for role in ('head', 'builder', 'reviewer')}

def save(roles):
    roles = validate(roles)
    with LOCK:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = PATH.with_suffix('.tmp')
        temp.write_text(json.dumps(roles, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, PATH)
    return roles
