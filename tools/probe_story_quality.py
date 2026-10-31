"""Authorized story-only model comparison; never runs tools or sends QQ messages."""
import ast
import asyncio
import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fllbot.config import ConfigStore, load_config_dict
from fllbot.llm.client import LLMClient
from fllbot.llm.personas import PersonaStore


async def main():
    print('Loading story-only probe', flush=True)
    # Only configuration is read from the DB. No private history or identifiers are sent.
    with sqlite3.connect(f"file:{(ROOT / 'data/fllbot.db').as_posix()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='config_overrides'").fetchone()
    store = ConfigStore(load_config_dict(root=ROOT))
    store.restore(json.loads(row[0]) if row else {})
    personas = PersonaStore([ROOT / 'fllbot/personas', ROOT / 'personas'])
    personas.load()
    system = personas.get(store.current.llm.persona).render(bot_name='猫猫', date='2026-10-31', weekday='六', time='23:13', user_name='测试用户')
    system += '\n\n' + store.current.llm.system_extra
    tree = ast.parse((ROOT / 'fllbot/builtin_plugins/agent/__init__.py').read_text('utf-8'))
    system += next(ast.literal_eval(n.value) for n in ast.walk(tree) if isinstance(n, ast.AugAssign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) and '自然语言智能体' in n.value.value)
    spec = importlib.util.spec_from_file_location('probe_voice', ROOT / 'plugins/voice.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    voice = module.plugin.ai_tools[0]
    original = [{'type': 'function', 'function': {'name': voice.qualified_name, 'description': voice.description, 'parameters': voice.parameters}}]
    neutral = copy.deepcopy(original)
    neutral[0]['function']['parameters']['properties']['text']['description'] = '要朗读的完整文字；长度根据用户任务决定。'
    revised = system.replace('默认回答简短（一两句话）；对方明确要求展开时再详细说明。', '日常闲聊简短自然；故事、解释和创作按任务需要完整展开。用户表示太短或意犹未尽时，应补充或继续。不要用人设或困倦代替完成请求。')
    # These are exclusively the story exchanges supplied by the user in screenshots.
    story = [
        {'role':'user','content':'猫讲个故事给我听'},
        {'role':'assistant','content':'从前有只猫守着一盏灯，等人回来等到灯灭了，就自己睡了。'},
        {'role':'user','content':'没了？'},
        {'role':'assistant','content':'没了，猫困了。'},
        {'role':'user','content':'666'},
        {'role':'assistant','content':'嗯。'},
        {'role':'user','content':'用语音讲故事给我听'},
        {'role':'assistant','content':'从前有只猫守着一盏灯，等人回来，等到灯灭了，就自己睡了。'},
        {'role':'user','content':'用语音讲个故事给我听'},
    ]
    cases = [('text_original', system, story[:1], None), ('feedback_original', system, story[:3], None)]
    for repeat in range(2):
        cases += [(f'voice_original_{repeat+1}', system, story, original), (f'voice_neutral_schema_{repeat+1}', system, story, neutral), (f'voice_revised_style_{repeat+1}', revised, story, neutral)]
    report = {'model': store.current.llm.model, 'max_tokens': store.current.llm.max_tokens, 'temperature': store.current.llm.temperature, 'scope': 'Only user-supplied story conversation and current prompts. Direct model calls; voice tool advertised but never executed. Excludes unrelated production history and web tools.', 'results': []}
    output = ROOT / 'data/diagnostics/story-quality.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    client = LLMClient(lambda: store.current.llm)
    try:
        for name, prompt, history, tools in cases:
            print('Requesting ' + name, flush=True)
            try:
                response = await client.chat_result([{'role':'system','content':prompt}, *history], provider='default', max_tokens=store.current.llm.max_tokens, tools=tools, timeout=60)
                spoken = []
                for call in response.tool_calls:
                    try:
                        spoken.append(json.loads(call['function']['arguments']).get('text', ''))
                    except (ValueError, KeyError):
                        pass
                result = {'case':name,'text':response.text,'spoken':spoken,'chars':len(response.text)+sum(map(len,spoken)),'finish_reason':response.finish_reason,'usage':response.usage,'model':response.model}
            except Exception as exc:
                result = {'case':name,'error_type':type(exc).__name__}
            report['results'].append(result)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
