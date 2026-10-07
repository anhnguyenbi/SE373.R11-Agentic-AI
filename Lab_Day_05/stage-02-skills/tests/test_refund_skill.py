import json

from langchain_core.messages import AIMessage, HumanMessage

import paths
from agent import build_agent, load_catalog, system_prompt
from observer import Observer
from tests.conftest import ScriptedChatModel
from tests.test_agent import stream_events


def test_refund_catalog_is_discovered_without_loading_body(lab_dirs):
    catalog = load_catalog()
    skill = next(item for item in catalog.skills if item.name == 'refund-policy')
    assert skill.location == 'skills/refund-policy/SKILL.md'
    assert catalog.diagnostics == []
    prompt = system_prompt()
    skill_text = (paths.WORKSPACE_DIR / skill.location).read_text(encoding='utf-8')
    body = skill_text.split('---', 2)[2].strip()
    assert skill.location in prompt
    assert body not in prompt
    assert 'policy-before-oct.md' not in prompt
    assert 'policy-from-oct.md' not in prompt


def test_loaded_skill_reference_and_renamed_policies_reach_context(lab_dirs):
    policy_dir = paths.WORKSPACE_DIR / 'data/policies'
    renamed_paths = []
    for index, file in enumerate(sorted(policy_dir.glob('*.md'))):
        renamed = file.with_name(f'revision-{index + 91}.md')
        file.rename(renamed)
        renamed_paths.append(renamed.relative_to(paths.WORKSPACE_DIR).as_posix())
    model = ScriptedChatModel(responses=[
        AIMessage(content='', tool_calls=[{'name': 'read_file', 'args': {'path': 'skills/refund-policy/SKILL.md'}, 'id': 'skill'}]),
        AIMessage(content='', tool_calls=[{'name': 'read_file', 'args': {'path': 'skills/refund-policy/references/answer-template.md'}, 'id': 'reference'}, {'name': 'list_files', 'args': {'path': 'data/policies'}, 'id': 'discovery'}]),
        AIMessage(content='', tool_calls=[{'name': 'read_file', 'args': {'path': path}, 'id': f'policy-{index}'} for index, path in enumerate(renamed_paths)]),
        AIMessage(content='Đã nạp tài liệu kiểm thử.'),
    ])
    events, messages = stream_events(build_agent(model), [HumanMessage(content='Tôi mua ngày 02/10/2026, yêu cầu hoàn ngày 12/10/2026, chưa kích hoạt. Tôi có được hoàn không?')])
    observer = Observer(conversation_id='refund-context-test')
    observer.start_turn('refund-context-run')
    for event in events:
        observer.record(event['event'], event['data'])
    observer.sync_messages(messages)
    inventory = observer.inventory()
    assert [item['skill'] for item in inventory['skills']] == ['refund-policy']
    assert {item['path'] for item in inventory['resources']} == set(renamed_paths) | {'skills/refund-policy/references/answer-template.md'}
    final_snapshot = observer.snapshots[-1]
    tool_results = [json.loads(message['content']) for message in final_snapshot['messages'] if message['role'] == 'tool']
    discovered = next(result for result in tool_results if 'entries' in result)
    assert {entry['path'] for entry in discovered['entries']} == set(renamed_paths)
    assert not any('policy-before-oct.md' in message['content'] or 'policy-from-oct.md' in message['content'] for message in final_snapshot['messages'] if message['role'] == 'tool')
    assert all(item['first_request'] is not None for item in inventory['skills'])
