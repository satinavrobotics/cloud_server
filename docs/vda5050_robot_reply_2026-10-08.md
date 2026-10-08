Subject: Re: VDA5050 orders: what we need from the server for multi-waypoint missions (incl. offline operation)

Hi,

Thanks for the detailed review. We agree with all six points. Below is where each one stands on our side, with three small changes to the nodePolicy proposal and one question about the map frame.

1. Release the whole route in one order
- Already the case. Every node and edge goes out with released: true, and orderUpdateId is always 0.
- We never use order updates. A re-route is always a cancelOrder followed by a new order with a new orderId.

2. allowedDeviationXY
- We will send it on every node:
  - 0.35 m for pass-through nodes;
  - 0.10 m for the last node and for any node that carries actions (docking, load stations).
- Both values will be configurable on our side, so we can tune them after the joint test.
- Today the planner sends 0.2 m and the start node (the robot's own pose) sends 0. We are fixing both.

3. nodePolicy
- The actionType, blockingType "NONE", the parameter names and the defaults are fine with us. Three changes:
  a) If skippable is true and corridorWidth is missing, please treat it as "no skip", not as "skip without a corridor check". We will always send corridorWidth together with skippable: true, so this costs nothing and fails safe.
  b) Please report a skip with its own errorType, e.g. "nodeSkipped" (errorLevel WARNING, errorReferences nodeId). We match on "edgeBlocked" today, and a separate type means a skip can never be mistaken for a block.
  c) We will never put nodePolicy on the start node (sequenceId 0, the robot's current pose).
- Rollout:
  - Phase 1: we send nodePolicy with maxWaitS (default 10 s) on pass-through nodes, and skippable false everywhere.
  - Phase 2: our graph does not yet record which nodes are doorways, the only way through a corridor, or open area, so we cannot yet decide safely which nodes are skippable. We will add that to the graph, then send skippable: true only on open-area pass-through nodes without actions, never on the last node, and always with corridorWidth.
- Question: please confirm the robot does not report nodePolicy in actionStates, or that it reports it FINISHED immediately. Our dispatcher must not wait on it as a mission action.

4. Robot reports
- We will:
  - handle the nodeSkipped WARNING without failing or pausing the mission, and show the skipped node to the operator;
  - store the per-node informations (offset, seen blocked, not observed) as advisory notes, show them, and not re-route on them;
  - handle progress that jumps several nodes at once in the first state after a reconnect. We will add tests for this.
- edgeBlocked with nodeId and edgeId is already handled.
- We found that our mission timeout kept running while the robot was offline. A long offline stretch could fail the mission and send a cancelOrder to a robot that was offline. We will pause the timeout while the robot is offline.
- To make the notes machine-readable, we propose these infoTypes:
  - nodeOffset, with infoReferences nodeId, offsetX, offsetY and offsetTheta (in the order's frame);
  - nodeBlocked, with infoReferences nodeId;
  - areaNotObserved, with infoReferences nodeId.
  Please confirm them or send yours. We will use the offsets to detect a frame error automatically.
- We will also handle the robot listing nodePolicy entries in actionStates in any order. Today an entry that is not an instant action could hide a cancelOrder acknowledgement behind it.

5. Re-routing while connected
- Most of this is already in place since the 2026-10-08 run:
  - no new order is sent while one of our cancelOrders is still open, so a re-route is one cancelOrder and then one new order;
  - an order the robot has not picked up is re-sent with back-off (1 s doubling to 8 s), not on every state message;
  - more than 5 new order versions within 60 s now fails the mission instead of looping;
  - a new route always gets a new orderId, and we never reuse an orderId/orderUpdateId with different content.
- We found the likely cause of "the next order went straight back to the blocked node":
  - when the robot reports edgeBlocked and drops its order (missionStatus "canceled"), our dispatcher took it as a dropped order and re-sent the remaining route, blocked node included;
  - we will stop that, and after an edgeBlocked we will wait for a re-route instead.
- We will also:
  - stop treating a "canceled" status as current while the robot still reports nodeStates of the order;
  - cap identical resends at 3;
  - wait about 2 s before cancelling an order version we have only just sent.
- Still to do: after a node is reported blocked, we will keep it out of new routes for 10 minutes (configurable). If no route exists without it, the re-route fails with a clear error and does not go back through the node.
- Note: if the robot has not picked up an order, we may still re-send exactly the same order (same orderId, orderUpdateId and content) with back-off. Per VDA5050 the robot should ignore the duplicate. Please confirm that your client does.

6. Map frame
- Graph nodes are stored in the map frame. Before sending, we convert them into the robot's session frame with inverse(map_T_session).
- One inconsistency on our side: we still label the converted nodes with mapId = the map name, while the start node (the robot's pose) goes out with an empty mapId. We will fix the labelling so that every node in an order has the same mapId.
- In the client checkout we have (sati_vda5050_client, 2026-09-30), nodePosition.mapId is ignored and nodes are driven in your map_frame_. If the deployed version does the same, the label does not cause a double transform today.
- A second candidate for a constant offset: when a robot relocalizes on a stored map, we currently assume map_T_session is identity. That is wrong if the robot's map was built in a different mapping session than the graph. A stale placement after a robot restart we did not detect would give the same effect.
- Questions:
  a) In which frame do you expect nodePosition: the stored map frame, or the robot's current session frame?
  b) Will you honour nodePosition.mapId? What should agvPosition.mapId be? Today it is "map".
  c) Is the deployed client the same as the 2026-09-30 checkout here?
- We will log the transform we apply to each order and compare it with your node offsets. If your offsets match our map_T_session translation, or the identity assumption, that settles it.

Smaller questions
- Start node (sequenceId 0, the robot's pose): we plan to send allowedDeviationXY 0.35 m, so the robot does not try to re-reach its own pose. Is that right?
- allowedDeviationTheta: we plan π (heading free) on pass-through nodes and 0.785 rad on the last node. Do you use theta on pass-through nodes at all?
- nodePolicy: we will send it only when your factsheet lists nodePolicy under customActions (it can also be forced on from our side). Can you add it there?
- Values in nodePolicy actionParameters will be typed JSON (true / 10 / 1.0). Other actions keep string values as today.

Joint test
- Yes please, we would like to run one against your wire test server.
- We will tell you when phase 1 (deviation values, maxWaitS, skip/notes handling, blocked-node exclusion, frame fix) is ready.
- Settling point 6 is the main goal of that test.

Thanks!
