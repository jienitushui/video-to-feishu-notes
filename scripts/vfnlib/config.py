from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Records are read/written by field name, optionally mapped in config.json.
FIELDS = {
    'url': ('视频链接', 1), 'enabled': ('开始生成', 7),
    'status': ('处理状态', 3), 'stage': ('当前阶段', 1),
    'title': ('视频标题', 1), 'platform': ('视频平台', 1),
    'category': ('学习分类', 1), 'requirements': ('笔记要求', 1),
    'document': ('笔记文档', 15), 'summary': ('内容摘要', 1),
    'regenerate': ('重新生成', 7), 'completed_at': ('完成时间', 5),
    'error': ('错误信息', 1), 'result': ('结果来源', 1),
    'source_key': ('视频唯一标识', 1), 'fingerprint': ('输入指纹', 1),
    'run_id': ('执行标识', 1), 'updated_at': ('最近更新时间', 5),
    'attempts': ('尝试次数', 2), 'version': ('生成版本', 2),
    'document_id': ('工作文档ID', 1), 'state': ('任务检查点', 1),
}
STATUSES = ['待处理', '处理中', '已完成', '失败', '待人工处理']


class Config:
    def __init__(self, data, path=None):
        self.data = data
        self.path = Path(path).resolve() if path else ROOT / 'config.json'
        if self.get('publish_target', 'folder') not in ('folder', 'wiki'):
            raise ValueError('publish_target must be folder or wiki')
        unknown = set(data.get('fields', {})) - set(FIELDS)
        if unknown:
            raise ValueError('Unknown field mappings: ' + ', '.join(sorted(unknown)))
        names = [self.field(k) for k in FIELDS]
        if len(names) != len(set(names)):
            raise ValueError('Field mappings must use distinct field names')
        if self.get('base_url', 'https://open.feishu.cn') not in (
                'https://open.feishu.cn', 'https://open.larksuite.com'):
            raise ValueError('base_url must be an official Feishu/Lark API host')

    @classmethod
    def load(cls, path=None):
        target = Path(path or os.environ.get('VFN_CONFIG') or ROOT / 'config.json')
        if not target.is_file():
            raise ValueError('Missing config: copy config.example.json to config.json and configure Feishu')
        return cls(json.loads(target.read_text(encoding='utf-8-sig')), target)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def require(self, key):
        value = self.get(key)
        if not value or str(value).startswith('YOUR_'):
            raise ValueError('Configure ' + key + ' in ' + str(self.path))
        return value

    def field(self, key):
        return self.data.get('fields', {}).get(key, FIELDS[key][0])

    def destination(self):
        if self.get('publish_target', 'folder') == 'wiki':
            return {'type': 'wiki', 'space_id': str(self.require('wiki_space_id')),
                    'parent_node_token': self.require('wiki_parent_node_token')}
        return {'type': 'folder', 'folder_token': self.require('folder_token')}

    def local_path(self, key, default):
        path = Path(self.get(key, default)).expanduser()
        return path.resolve() if path.is_absolute() else (self.path.parent / path).resolve()
