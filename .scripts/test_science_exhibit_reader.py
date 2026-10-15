#!/usr/bin/env python3
"""Local regression tests; never contact a model."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

import science_exhibit_reader as reader


def answer():
    return {
        'first_impression': 'TEST-ONLY: 一个故事',
        'retelling': {key: '未读懂' for key in ('question', 'method', 'finding', 'importance')},
        'visual_meanings': ['上方的图，未读懂'],
        'confusions': ['底部字很小，看不清，未读懂'],
        'suggestions': ['底部小字，建议放大字，理由：可读'],
        'unanswered_questions': [], 'reading_path': '从上到下',
    }


def rejected(operation):
    try:
        operation()
    except reader.ReaderError:
        return
    raise AssertionError('Expected validation failure')


def run_tests(root):
    image = root / 'temp' / 'poster.png'
    image.parent.mkdir()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('secret', 'metadata-must-not-be-transmitted')
    Image.new('RGB', (200, 300), 'white').save(image, pnginfo=metadata)
    original = image.read_bytes()
    prompts = []

    def fake_call(model, images, prompt, config):
        assert len(images) == 1
        assert 'metadata-must-not-be-transmitted' not in images[0].read_bytes().decode('latin1')
        with Image.open(images[0]) as sent:
            assert not sent.info
        assert images[0].read_bytes() != original
        prompts.append(prompt)
        return {'result': answer(), 'usage': {'total_tokens': 10, 'secret': 'test-secret'}}

    settings = {'VISUAL_QA_API_BASE': 'https://test.invalid/v1', 'VISUAL_QA_API_KEY': 'test-secret',
                'VISUAL_QA_MODEL': 'test-model'}
    with patch.object(reader, 'REPO', root), patch.object(reader, 'load_visual_env', return_value=settings), \
            patch.object(reader, 'call_json_vision', side_effect=fake_call) as remote:
        rejected(lambda: reader.review_poster(image))
        assert remote.call_count == 0
        for protected in ('raw', 'wiki', 'private', 'inbox', 'sources', 'source-local'):
            folder = root / protected
            folder.mkdir()
            blocked = folder / 'poster.png'
            blocked.write_bytes(original)
            rejected(lambda: reader.review_poster(blocked, allow_remote=True))
        linked = image.parent / 'linked.png'
        linked.symlink_to(image)
        rejected(lambda: reader.review_poster(linked, allow_remote=True))
        linked_dir = root / 'linked'
        linked_dir.symlink_to(image.parent, target_is_directory=True)
        rejected(lambda: reader.review_poster(linked_dir / image.name, allow_remote=True))
        rejected(lambda: reader.review_poster(root / 'temp' / '..' / 'temp' / image.name, allow_remote=True))
        rejected(lambda: reader.review_poster(root.parent / 'outside.png', allow_remote=True))
        malformed = image.parent / 'invalid.png'
        malformed.write_text('not PNG')
        rejected(lambda: reader.review_poster(malformed, allow_remote=True))
        rejected(lambda: reader.review_poster(image, allow_remote=True, timeout=0))
        assert remote.call_count == 0
        result = reader.review_poster(image, allow_remote=True)
        report = json.loads(Path(result['report']).read_text())
        assert result['status'] == 'completed' and result['completed_readers'] == 2
        assert remote.call_count == 2 and prompts[0] != prompts[1]
        assert report['prompt_version'] == 'science-exhibit-blind-reader-v4'
        for persona, prompt in zip(reader.PERSONAS, prompts):
            assert prompt == reader.reader_prompt(persona)
            assert '为什么值得关心、想实现什么、困难在哪里' in prompt
            assert '图上未讲清时直说，不补写动机' in prompt
            assert '哪里需要跳步猜测' in prompt
            assert all(forbidden not in prompt for forbidden in ('MPS', 'MPE', '潜艇', '纠缠', 'PRL', 'TEST-ONLY'))
        assert image.read_bytes() == original
        assert report['input_sha256'] == reader.digest(original)
        assert 'test-secret' not in json.dumps(report)
        assert all(entry['usage'] == {'total_tokens': 10} for entry in report['readers'])

        with patch.object(reader, 'call_json_vision', side_effect=[RuntimeError('test-secret'), {'result': answer()}]):
            partial = reader.review_poster(image, allow_remote=True)
        assert partial['status'] == 'partial' and partial['completed_readers'] == 1
        assert 'test-secret' not in Path(partial['report']).read_text()
        with patch.object(reader, 'call_json_vision', return_value={'result': {'score': 100}, 'usage': {'total_tokens': 15}}):
            failed = reader.review_poster(image, allow_remote=True)
        assert failed['status'] == 'failed'
        failed_report = json.loads(Path(failed['report']).read_text())
        assert all(entry['usage'] == {'total_tokens': 15} for entry in failed_report['readers'])

        def changing_call(*args):
            Image.new('RGB', (200, 300), 'blue').save(image)
            return {'result': answer()}

        with patch.object(reader, 'call_json_vision', side_effect=changing_call):
            drifted = reader.review_poster(image, allow_remote=True)
        assert drifted['status'] == 'invalidated' and drifted['input_changed']
        receipt_root = root / 'temp' / 'science-exhibit' / 'reader-reviews'
        moved_root = receipt_root.with_name('moved-reviews')
        receipt_root.rename(moved_root)
        receipt_root.symlink_to(moved_root, target_is_directory=True)
        rejected(lambda: reader.review_poster(image, allow_remote=True))
        with patch.object(reader, 'load_visual_env', return_value={}):
            rejected(lambda: reader.review_poster(image, allow_remote=True))

    for mutate in (
        lambda data: data.update(extra='not allowed'),
        lambda data: data.update(first_impression=''),
        lambda data: data.update(unanswered_questions='not a list'),
        lambda data: data['retelling'].update(question=None),
        lambda data: data.update(visual_meanings=[{'extra': 'not allowed'}]),
        lambda data: data.update(suggestions=[{}] * 13),
    ):
        data = copy.deepcopy(answer())
        mutate(data)
        rejected(lambda: reader.validate_response(data))


def main():
    assert reader.safe_error(reader.VisualQAError('vision API HTTP 400')) == 'vision API HTTP 400'
    assert 'test-secret' not in reader.safe_error(reader.VisualQAError('test-secret'))
    with tempfile.TemporaryDirectory(prefix='science-exhibit-reader-') as directory:
        run_tests(Path(directory).resolve())
    print('science exhibit reader regression: PASS (mock API, not human acceptance)')


if __name__ == '__main__':
    main()
