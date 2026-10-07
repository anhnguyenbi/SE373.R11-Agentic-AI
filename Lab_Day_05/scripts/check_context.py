from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'stage-02-skills'))

from langchain_core.messages import AIMessage, HumanMessage

import paths
from agent import build_agent
from observer import Observer
from tests.conftest import ScriptedChatModel
from tools import list_files


def main() -> int:
    evidence = ROOT / 'evidence'
    evidence.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lab05-context-') as temporary:
        workspace = Path(temporary) / 'workspace'
        shutil.copytree(ROOT / 'stage-02-skills/workspace', workspace)
        paths.WORKSPACE_DIR = workspace
        for index, file in enumerate(sorted((workspace / 'data/policies').glob('*.md'))):
            file.rename(file.with_name(f'revision-{index + 91}.md'))
        discovered_paths = [entry['path'] for entry in json.loads(list_files.invoke({'path': 'data/policies'}))['entries']]
        model = ScriptedChatModel(responses=[
            AIMessage(content='', tool_calls=[{'id': 'skill', 'name': 'read_file', 'args': {'path': 'skills/refund-policy/SKILL.md'}}]),
            AIMessage(content='', tool_calls=[{'id': 'reference', 'name': 'read_file', 'args': {'path': 'skills/refund-policy/references/answer-template.md'}}, {'id': 'discovery', 'name': 'list_files', 'args': {'path': 'data/policies'}}]),
            AIMessage(content='', tool_calls=[{'id': f'policy-{index}', 'name': 'read_file', 'args': {'path': path}} for index, path in enumerate(discovered_paths)]),
            AIMessage(content='Đã nạp tài liệu kiểm thử; model giả lập không đánh giá điều kiện hoàn tiền.'),
        ])
        question = 'Tôi mua ngày 02/10/2026, yêu cầu hoàn ngày 12/10/2026, chưa kích hoạt. Tôi có được hoàn không?'
        history = [HumanMessage(content=question)]
        observer = Observer(conversation_id='offline-context-check')
        observer.start_turn('offline-context-run')
        observer.record('user_submitted', {'content': question})
        for mode, chunk in build_agent(model).stream({'messages': history}, stream_mode=['updates', 'custom']):
            if mode == 'custom' and chunk.get('observer'):
                observer.record(chunk['event'], chunk['data'])
            elif mode == 'updates':
                for update in chunk.values():
                    if isinstance(update, dict) and update.get('messages'):
                        history.extend(update['messages'])
        observer.sync_messages(history)
        observer.record('run_completed', {'answer': history[-1].content})
        inventory = observer.inventory()
        assert [item['skill'] for item in inventory['skills']] == ['refund-policy']
        assert {item['path'] for item in inventory['resources']} == set(discovered_paths) | {'skills/refund-policy/references/answer-template.md'}
        assert len(observer.snapshots[0]['messages']) == 1
        events = [dict(event, execution_mode='scripted-mock') for event in observer.events]
        (evidence / 'stage-02_context_mock.jsonl').write_text(''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events), encoding='utf-8')
        export = {'execution_mode': 'scripted-mock', **observer.export()}
        (evidence / 'stage-02_context_mock.json').write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Kiểm tra truyền skill, reference, listing và nội dung file vào context đạt; đây là trace mock.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
