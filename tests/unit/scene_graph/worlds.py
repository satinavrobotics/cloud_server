"""The reference worlds: each returns (world, path in metres)."""

from .synth import box, carve, room


def empty_room():
    w = room(8, 6)
    return w, [(1, 1), (7, 1), (7, 5), (1, 5), (4, 3)]


def corridor():
    w = room(20, 1.5)
    return w, [(0.5, 0.75), (19.5, 0.75)]


def l_room():
    w = room(10, 10)
    box(w, 3, 3, 10, 10)                          # fills the upper right: an L of width 3
    return w, [(1, 1), (1, 8), (1, 1), (8, 1), (1.5, 1.5)]


def two_rooms_door(door_m=0.9):
    w = room(14, 6)
    box(w, 6.9, 0, 7.1, 6)                         # wall between the two rooms
    y0 = 3 - door_m / 2
    carve(w, 6.85, y0, 7.15, y0 + door_m)
    return w, [(1, 3), (6, 3), (8, 3), (13, 3), (10, 5), (10, 1), (4, 1), (4, 5)]


def pillar_room():
    w = room(10, 8)
    box(w, 3.5, 2.5, 6.5, 5.5)                     # a 3 x 3 m block in the middle
    return w, [(1, 1), (9, 1), (9, 7), (1, 7), (1, 1), (5, 1)]


def ring_corridor():
    w = room(12, 12)
    box(w, 2.5, 2.5, 9.5, 9.5)                     # a block, leaving a 2.5 m ring around it
    return w, [(1.25, 1.25), (10.75, 1.25), (10.75, 10.75), (1.25, 10.75), (1.25, 1.25), (5, 1.25)]


ALL = dict(empty_room=empty_room, corridor=corridor, l_room=l_room, two_rooms_door=two_rooms_door,
           pillar_room=pillar_room, ring_corridor=ring_corridor)
