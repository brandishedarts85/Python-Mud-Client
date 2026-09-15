from mapper import RouteStep
from mapper_walker import MapperWalker


def step(exit_id, source, dest, direction, command=None):
    return RouteStep(exit_id, source, dest, direction, command or direction, 1.0)


def test_walker_sends_one_step_and_requires_room_plus_prompt():
    sent=[]; now=[10.0]
    w=MapperWalker(lambda cmd: sent.append(cmd) or True, clock=lambda:now[0])
    w.start((step(1,1,2,'north'), step(2,2,3,'east')), source_room_id=1, destination_room_id=3)
    assert sent == ['north']
    w.note_prompt(); assert sent == ['north']
    w.note_room(2); assert sent == ['north','east']
    w.note_room(3); assert w.active
    w.note_prompt(); assert not w.active and w.status.message == 'arrived'


def test_walker_stops_on_divergence():
    sent=[]
    w=MapperWalker(lambda cmd: sent.append(cmd) or True)
    w.start((step(1,1,2,'north'),), source_room_id=1, destination_room_id=2)
    w.note_room(99)
    assert not w.active
    assert 'diverged' in w.status.message


def test_duplicate_source_observation_does_not_abort():
    w=MapperWalker(lambda _cmd: True)
    w.start((step(1,1,2,'north'),), source_room_id=1, destination_room_id=2)
    w.note_room(1)
    assert w.active


def test_walker_times_out_without_both_acknowledgements():
    now=[0.0]
    w=MapperWalker(lambda _cmd: True, step_timeout=2.0, clock=lambda:now[0])
    w.start((step(1,1,2,'north'),), source_room_id=1, destination_room_id=2)
    w.note_room(2)
    now[0]=2.1
    w.check_timeout()
    assert not w.active and 'timed out' in w.status.message


def test_send_failure_stops_walker():
    w=MapperWalker(lambda _cmd: False)
    w.start((step(1,1,2,'north'),), source_room_id=1, destination_room_id=2)
    assert not w.active
    assert 'not sent' in w.status.message


def test_arrival_reports_exact_completed_step_count():
    w=MapperWalker(lambda _cmd: True)
    w.start((step(1,1,2,'north'),), source_room_id=1, destination_room_id=2)
    w.note_room(2); w.note_prompt()
    assert w.status.step_index == 1
    assert w.status.step_count == 1


def test_door_precommand_waits_for_prompt_before_movement():
    sent=[]
    door = RouteStep(1, 1, 2, 'north', 'n', 1.0, 'door', 'open north')
    w=MapperWalker(lambda cmd: sent.append(cmd) or True)
    w.start((door,), source_room_id=1, destination_room_id=2)
    assert sent == ['open north']
    assert w.status.phase == 'preparing'
    w.note_room(1)
    assert sent == ['open north']
    w.note_prompt()
    assert sent == ['open north', 'n']
    assert w.status.phase == 'moving'
    w.note_room(2)
    assert w.active
    w.note_prompt()
    assert not w.active and w.status.message == 'arrived'


def test_divergence_reroutes_only_after_prompt_synchronization():
    sent=[]
    initial = (step(1,1,2,'north'), step(2,2,4,'east'))
    replacement = (step(3,3,4,'south'),)
    calls=[]
    def reroute(source, dest):
        calls.append((source,dest))
        return replacement
    w=MapperWalker(lambda cmd: sent.append(cmd) or True, reroute=reroute)
    w.start(initial, source_room_id=1, destination_room_id=4)
    assert sent == ['north']
    w.note_room(3)
    assert calls == [(3,4)]
    assert w.active and w.status.phase == 'reroute_wait'
    assert sent == ['north']
    w.note_prompt()
    assert sent == ['north','south']
    assert w.status.reroute_count == 1
    w.note_room(4); w.note_prompt()
    assert w.status.message == 'arrived'


def test_prompt_before_divergent_room_is_preserved_for_reroute_barrier():
    sent=[]
    replacement=(step(9,3,4,'south'),)
    w=MapperWalker(lambda cmd: sent.append(cmd) or True, reroute=lambda _s,_d: replacement)
    w.start((step(1,1,2,'north'), step(2,2,4,'east')), source_room_id=1, destination_room_id=4)
    w.note_prompt()
    assert sent == ['north']
    w.note_room(3)
    assert sent == ['north','south']


def test_reroute_limit_stops_repeated_divergence():
    sent=[]
    routes={3:(step(3,3,4,'south'),), 5:(step(5,5,4,'west'),)}
    w=MapperWalker(lambda cmd: sent.append(cmd) or True,
                   reroute=lambda source,_dest: routes.get(source), max_reroutes=1)
    w.start((step(1,1,2,'north'), step(2,2,4,'east')), source_room_id=1, destination_room_id=4)
    w.note_room(3); w.note_prompt()
    assert sent == ['north','south']
    w.note_room(5)
    assert not w.active
    assert 'reroute limit reached' in w.status.message


def test_divergent_move_directly_to_destination_waits_for_prompt_then_arrives():
    w=MapperWalker(lambda _cmd: True, reroute=lambda _s,_d: None)
    w.start((step(1,1,2,'north'), step(2,2,4,'east')), source_room_id=1, destination_room_id=4)
    w.note_room(4)
    assert w.active and w.status.phase == 'reroute_wait'
    w.note_prompt()
    assert not w.active and w.status.message == 'arrived'


def test_route_steps_snapshot_is_visible_only_while_active():
    from mapper import RouteStep
    sent=[]
    walker=MapperWalker(lambda cmd: sent.append(cmd) or True)
    step=RouteStep(7,1,2,'north','north',1.0)
    walker.start((step,), source_room_id=1, destination_room_id=2)
    assert walker.route_steps == (step,)
    walker.cancel('done')
    assert walker.route_steps == ()
