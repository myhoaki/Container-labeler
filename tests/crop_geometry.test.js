const test = require('node:test');
const assert = require('node:assert/strict');
const { rectFromDrag, remapBoxes } = require('../app/static/crop_geometry.js');

test('crop drag clips at every source-image edge', () => {
  assert.deepEqual(rectFromDrag({x:-20,y:-8},{x:120,y:80},{x:0,y:0,scale:1},100,80),
    {x:0,y:0,width:100,height:80});
});

test('crop coordinates stay in source pixels after zoom and pan', () => {
  assert.deepEqual(rectFromDrag({x:60,y:30},{x:140,y:90},{x:40,y:10,scale:2},100,80),
    {x:10,y:10,width:40,height:30});
});

test('YOLO boxes are normalized relative to cropped dimensions', () => {
  const boxes=[[.5,.5,.4,.4]];
  assert.deepEqual(remapBoxes(boxes,100,100,{x:25,y:25,width:50,height:50}),[[.5,.5,.8,.8]]);
  assert.deepEqual(boxes,[[.5,.5,.4,.4]],'source annotation state is not mutated');
});

test('crop clips partial annotations and removes boxes outside the crop', () => {
  assert.deepEqual(remapBoxes([[.25,.5,.5,.5],[.9,.9,.1,.1]],100,100,{x:25,y:25,width:50,height:50}),
    [[.25,.5,.5,1]]);
});

test('zero-area edge drags remain invalid', () => {
  const r=rectFromDrag({x:110,y:20},{x:120,y:50},{x:0,y:0,scale:1},100,80);
  assert.equal(r.width,0);
});
