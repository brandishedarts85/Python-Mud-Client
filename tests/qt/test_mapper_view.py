import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
PySide6 = pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication

from mapper import Exit, Room, RouteStep
from qt.map_view import MapperGraphicsView


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_graphical_mapper_renders_rooms_edges_and_selection(app):
    selected=[]
    view=MapperGraphicsView(selected.append)
    rooms=(Room(1,'A',x=0,y=0), Room(2,'B',x=1,y=0))
    exits=(Exit(10,1,2,'east','east'),)
    route=(RouteStep(10,1,2,'east','east',1.0),)
    view.render_map(rooms, exits, current_room_id=1, route_steps=route, selected_room_id=2)
    assert set(view._room_items) == {1,2}
    assert view._room_items[2].isSelected()
    assert not view.scene().itemsBoundingRect().isNull()
