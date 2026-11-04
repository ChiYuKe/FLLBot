import asyncio

from helpers import make_bot


async def test_waiting_body_writes_recheck_account_epoch_under_lock(tmp_path):
    bot = await make_bot(tmp_path,builtin=False)
    store = bot.storage
    epoch = bot.bots.epoch(10001)
    generation = store.conversation_generation
    source = await store.memory.append(10001,20002,'user','original',created_at=100)
    sources = await store.memory.recent(10001,20002,source+1)
    await store.prepare_delivery('sent-fact',10001,'group',20002,'candidate',['confirmed'],generation,
                                  metadata={'account_epoch':epoch})
    await store.update_delivery('sent-fact',0,'sending')
    await store.claim_tool('tool-fact',10001,'group',20002,1,'example','side_effecting',generation,'turn','call','hash')
    entered = [asyncio.Event() for _ in range(6)]
    async def waiting(index, operation):
        entered[index].set()
        return await operation()
    try:
        async with store.data_lock:
            tasks = [asyncio.create_task(waiting(index,operation)) for index,operation in enumerate([
                lambda:store.add_chat_message('group',20002,'user','stale',self_id=10001,generation=generation,account_epoch=epoch),
                lambda:store.memory.append(10001,20002,'user','stale',created_at=101,generation=generation,account_epoch=epoch),
                lambda:store.memory.save_summary(10001,20002,sources,'1111',[{'topic':'stale'}],generation=generation,account_epoch=epoch),
                lambda:store.finish_tool('tool-fact','succeeded',output='stale',generation=generation,account_epoch=epoch),
                lambda:store.prepare_delivery('stale-plan',10001,'group',20002,'stale',['stale'],generation,metadata={'account_epoch':epoch}),
                lambda:store.update_delivery('sent-fact',0,'sent',message_id='123'),
            ])]
            await asyncio.gather(*(event.wait() for event in entered))
            await bot.bots.update(10001,enabled=False)
            await bot.bots.update(10001,enabled=True)
        await asyncio.gather(*tasks)
        assert not await store.recent_chat('group',20002,self_id=10001)
        assert [row['content'] for row in await store.memory.recent(10001,20002,999)] == ['original']
        assert not await store.memory.summaries(10001,20002,'1111',0,999)
        assert (await (await store.db.execute('SELECT count(*) FROM tool_results')).fetchone())[0] == 0
        assert (await (await store.db.execute("SELECT count(*) FROM reply_deliveries WHERE delivery_id='stale-plan'")).fetchone())[0] == 0
        # Completed external facts survive; only projection into visible history is blocked.
        receipt = await (await store.db.execute("SELECT status,message_id FROM delivery_parts WHERE delivery_id='sent-fact'")).fetchone()
        assert (receipt['status'],receipt['message_id']) == ('sent','123')
        tool = await (await store.db.execute("SELECT status FROM tool_executions WHERE execution_id='tool-fact'")).fetchone()
        assert tool['status'] == 'succeeded'
    finally:
        await bot.stop()
