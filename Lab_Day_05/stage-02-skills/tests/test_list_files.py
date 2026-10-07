import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import paths
from agent import build_agent
from tests.conftest import ScriptedChatModel
from tests.test_agent import stream_events
from tools import list_files
from tools.files import _list


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'workspace'
    (root / 'data/nested').mkdir(parents=True)
    (root / 'data/z.md').write_text('z', encoding='utf-8')
    (root / 'data/a.md').write_text('a', encoding='utf-8')
    (root / 'data/nested/hidden.md').write_text('nested', encoding='utf-8')
    return root


def test_direct_entries_sorted_with_relative_paths(workspace):
    assert _list(workspace, 'data') == {
        'ok': True,
        'path': 'data',
        'entries': [
            {'name': 'a.md', 'path': 'data/a.md', 'type': 'file'},
            {'name': 'nested', 'path': 'data/nested', 'type': 'directory'},
            {'name': 'z.md', 'path': 'data/z.md', 'type': 'file'},
        ],
    }


def test_empty_directory_is_success(workspace):
    (workspace / 'empty').mkdir()
    assert _list(workspace, 'empty') == {'ok': True, 'path': 'empty', 'entries': []}


def test_workspace_root(workspace):
    assert _list(workspace, '.')['entries'] == [{'name': 'data', 'path': 'data', 'type': 'directory'}]


@pytest.mark.parametrize(('path', 'code'), [
    ('data/a.md', 'NOT_A_DIRECTORY'),
    ('missing', 'DIRECTORY_NOT_FOUND'),
    ('../outside', 'PATH_OUTSIDE_WORKSPACE'),
    ('data/../../outside', 'PATH_OUTSIDE_WORKSPACE'),
    ('~', 'PATH_OUTSIDE_WORKSPACE'),
    ('', 'INVALID_PATH'),
])
def test_invalid_paths_return_structured_errors(workspace, path, code):
    result = _list(workspace, path)
    assert result['ok'] is False
    assert result['error']['code'] == code
    assert result['error']['message']
    assert 'entries' not in result


def test_absolute_path_blocked_even_inside(workspace):
    assert _list(workspace, str(workspace / 'data'))['error']['code'] == 'PATH_OUTSIDE_WORKSPACE'


def test_symlink_to_external_directory_blocked(workspace):
    outside = workspace.parent / 'outside'
    outside.mkdir()
    (outside / 'dummy.md').write_text('dummy', encoding='utf-8')
    (workspace / 'external').symlink_to(outside, target_is_directory=True)
    assert _list(workspace, 'external')['error']['code'] == 'PATH_OUTSIDE_WORKSPACE'


def test_external_child_symlink_blocks_listing(workspace):
    outside = workspace.parent / 'dummy.md'
    outside.write_text('dummy', encoding='utf-8')
    (workspace / 'data/link.md').symlink_to(outside)
    assert _list(workspace, 'data')['error']['code'] == 'PATH_OUTSIDE_WORKSPACE'


def test_internal_symlink_returns_accessible_alias(workspace):
    (workspace / 'data/link.md').symlink_to(workspace / 'data/a.md')
    entry = next(item for item in _list(workspace, 'data')['entries'] if item['name'] == 'link.md')
    assert entry == {'name': 'link.md', 'path': 'data/link.md', 'type': 'file'}


def test_dangling_symlink_returns_error(workspace):
    (workspace / 'data/link.md').symlink_to(workspace / 'missing.md')
    assert _list(workspace, 'data')['error']['code'] == 'UNSUPPORTED_ENTRY'


def test_permission_error_is_structured(workspace, monkeypatch):
    def denied(self):
        raise PermissionError('denied')
    monkeypatch.setattr(type(workspace), 'iterdir', denied)
    assert _list(workspace, 'data')['error']['code'] == 'DIRECTORY_ACCESS_ERROR'


def test_tool_wrapper(workspace, monkeypatch):
    monkeypatch.setattr(paths, 'WORKSPACE_DIR', workspace)
    assert json.loads(list_files.invoke({'path': 'data'})) == _list(workspace, 'data')


def test_list_schema_and_result_reach_model(workspace, monkeypatch):
    monkeypatch.setattr(paths, 'WORKSPACE_DIR', workspace)
    model = ScriptedChatModel(responses=[
        AIMessage(content='', tool_calls=[{'id': 'list-1', 'name': 'list_files', 'args': {'path': 'data'}}]),
        AIMessage(content='Đã liệt kê.'),
    ])
    events, messages = stream_events(build_agent(model), [HumanMessage(content='Liệt kê thư mục data.')])
    requests = [event['data'] for event in events if event['event'] == 'model_request']
    schema = next(tool for tool in requests[0]['tools'] if tool['name'] == 'list_files')
    assert schema['parameters']['required'] == ['path']
    assert schema['parameters']['properties']['path']['type'] == 'string'
    tool_message = next(message for message in requests[1]['messages'] if message['role'] == 'tool')
    assert tool_message['name'] == 'list_files'
    assert json.loads(tool_message['content']) == _list(workspace, 'data')
    assert messages[-1].content == 'Đã liệt kê.'
