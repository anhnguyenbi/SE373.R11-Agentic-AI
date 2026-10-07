from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True, choices=('stage-01-files', 'stage-02-skills'))
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / args.stage))
    import paths
    from tools import list_files, read_file
    from observer import tool_schema

    evidence = ROOT / 'evidence'
    evidence.mkdir(exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='lab05-tools-') as temporary:
        root = Path(temporary)
        workspace = root / 'workspace'
        (workspace / 'data/nested').mkdir(parents=True)
        (workspace / 'empty').mkdir()
        (workspace / 'data/z.md').write_text('Dữ liệu giả Z', encoding='utf-8')
        (workspace / 'data/a.md').write_text('Dữ liệu giả A', encoding='utf-8')
        (workspace / 'data/nested/child.md').write_text('Dữ liệu giả', encoding='utf-8')
        (root / 'outside').mkdir()
        (root / 'outside/dummy.md').write_text('Dữ liệu giả', encoding='utf-8')
        (workspace / 'outside-link').symlink_to(root / 'outside', target_is_directory=True)
        paths.WORKSPACE_DIR = workspace
        cases = [
            ('valid-directory', 'data', None), ('file', 'data/a.md', 'NOT_A_DIRECTORY'),
            ('missing', 'missing', 'DIRECTORY_NOT_FOUND'), ('traversal', '../outside', 'PATH_OUTSIDE_WORKSPACE'),
            ('absolute', str(workspace / 'data'), 'PATH_OUTSIDE_WORKSPACE'),
            ('external-symlink', 'outside-link', 'PATH_OUTSIDE_WORKSPACE'), ('empty-directory', 'empty', None),
        ]
        for case, path, expected in cases:
            result = json.loads(list_files.invoke({'path': path}))
            if expected:
                assert result['ok'] is False and result['error']['code'] == expected
            else:
                assert result['ok'] is True
            if case == 'valid-directory':
                assert [entry['name'] for entry in result['entries']] == ['a.md', 'nested', 'z.md']
            results.append({'mode': 'direct-tool-execution', 'case': case, 'tool': 'list_files', 'arguments': {'path': path}, 'result': result, 'passed': True})
        (workspace / 'data/a.md').rename(workspace / 'data/renamed-41.md')
        listing = json.loads(list_files.invoke({'path': 'data'}))
        renamed = next(entry for entry in listing['entries'] if entry['name'] == 'renamed-41.md')
        content = json.loads(read_file.invoke({'path': renamed['path']}))
        assert content['content'] == 'Dữ liệu giả A'
        results.extend([
            {'mode': 'direct-tool-execution', 'case': 'renamed-discovery', 'tool': 'list_files', 'arguments': {'path': 'data'}, 'result': listing, 'passed': True},
            {'mode': 'direct-tool-execution', 'case': 'renamed-read', 'tool': 'read_file', 'arguments': {'path': renamed['path']}, 'result': content, 'passed': True},
        ])
    (evidence / f'{args.stage}_tool_checks.jsonl').write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in results), encoding='utf-8')
    (evidence / f'{args.stage}_list_files_schema.json').write_text(json.dumps(tool_schema(list_files), ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{args.stage}: {len(results)} kiểm tra trực tiếp đạt.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
