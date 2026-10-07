from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('stage-00-chat', 'stage-01-files', 'stage-02-skills')
CASES = {
    'A': 'Tôi mua ngày 28/09/2026, yêu cầu hoàn ngày 06/10/2026, chưa kích hoạt. Tôi có được hoàn không?',
    'B': 'Tôi mua ngày 02/10/2026, yêu cầu hoàn ngày 12/10/2026, chưa kích hoạt. Tôi có được hoàn không?',
    'A-renamed': 'Tôi mua ngày 28/09/2026, yêu cầu hoàn ngày 06/10/2026, chưa kích hoạt. Tôi có được hoàn không?',
    'B-renamed': 'Tôi mua ngày 02/10/2026, yêu cầu hoàn ngày 12/10/2026, chưa kích hoạt. Tôi có được hoàn không?',
    'missing-activation': 'Tôi mua ngày 02/10/2026, muốn hoàn ngày 12/10/2026.',
}


def run_worker(stage: str, case: str, output: Path) -> int:
    sys.path.insert(0, str(ROOT / stage))
    from langchain_core.messages import HumanMessage
    import agent
    import config
    import paths
    from observer import Observer
    from trace import TraceWriter

    settings, missing = config.load_settings()
    if missing:
        print('Thiếu cấu hình: ' + ', '.join(missing), file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lab05-') as temporary:
        workspace = Path(temporary) / 'workspace'
        original_names = {}
        if stage != 'stage-00-chat':
            shutil.copytree(ROOT / stage / 'workspace', workspace)
            paths.WORKSPACE_DIR = workspace
            if case.endswith('-renamed'):
                for index, file in enumerate(sorted((workspace / 'data/policies').iterdir())):
                    if file.is_file():
                        renamed = file.with_name(f'edition-{index + 31}.md')
                        original_names[file.name] = renamed.name
                        file.rename(renamed)
        conversation_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
        observer = Observer(conversation_id=conversation_id)
        observer.start_turn(run_id)
        writer = TraceWriter(output, stage, conversation_id, run_id, 1)
        manifest_path = output / f'{stage}_{case}.json'
        manifest = {
            'mode': 'live-provider', 'stage': stage, 'case': case,
            'question': CASES[case], 'model': settings.model_name,
            'conversation_id': conversation_id, 'renamed_files': original_names,
            'trace': writer.path.name, 'status': 'running',
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        history = [HumanMessage(content=CASES[case])]
        writer.write(observer.record('user_submitted', {'content': CASES[case]}))
        try:
            graph = agent.build_agent(agent.build_model(settings))
            for mode, chunk in graph.stream(
                {'messages': history}, stream_mode=['updates', 'custom'],
                config={'recursion_limit': config.RECURSION_LIMIT},
            ):
                if mode == 'custom' and chunk.get('observer'):
                    writer.write(observer.record(chunk['event'], chunk['data']))
                elif mode == 'updates':
                    for update in chunk.values():
                        if isinstance(update, dict) and update.get('messages'):
                            history.extend(update['messages'])
            observer.sync_messages(history)
            answer = next((message.content for message in reversed(history) if message.type == 'ai' and not message.tool_calls), '')
            writer.write(observer.record('run_completed', {'answer': answer}))
            manifest.update(status='completed', answer=answer, inventory=observer.inventory())
            (output / f'{stage}_{case}_context.json').write_text(json.dumps(observer.export(), ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'{stage} / {case}: {writer.path.name}')
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            if settings.api_key:
                error = error.replace(settings.api_key, '[REDACTED]')
            writer.write(observer.record('run_failed', {'error': error}))
            manifest.update(status='failed', error=error)
            print(f'{stage} / {case}: thất bại; xem manifest.', file=sys.stderr)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if manifest['status'] == 'completed' else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/live')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            parser.error('Không tìm thấy file cấu hình.')
        load_dotenv(args.env_file, override=True)
    if args.worker:
        return run_worker(args.stage, args.case, args.output.resolve())
    stages = [args.stage] if args.stage else STAGES
    failed = False
    for stage in stages:
        cases = [args.case] if args.case else (['A'] if stage == 'stage-00-chat' else list(CASES))
        for case in cases:
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--stage', stage, '--case', case, '--output', str(args.output.resolve())]
            result = subprocess.run(command, cwd=ROOT / stage, check=False)
            if result.returncode == 2:
                return 2
            failed = failed or result.returncode != 0
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
