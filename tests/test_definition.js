"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const editor = require("../chuantuo/static/definition.js");
const demo = () => editor.normalize(JSON.parse(fs.readFileSync(path.join(__dirname,"../examples/cross-layer/scenario.json"),"utf8")));

test("renaming a nested actor preserves containment, direct edges and private readers", () => {
  const scene = demo();
  scene.entities[0].fields[0].visibility.readers = ["engineer"];
  scene.subsystems.push({id:"lab",name:"实验组",owner_id:"engineer"});
  const renamed = editor.renameActor(scene,"engineer","设计工程师");
  assert.equal(editor.depths(renamed)["设计工程师"],2);
  assert.equal(renamed.subsystems.at(-1).owner_id,"设计工程师");
  assert.deepEqual(renamed.entities[0].fields[0].visibility.readers,["设计工程师"]);
  assert(renamed.influences.some(edge => edge.source === "设计工程师" && edge.target === "company"));
  assert(renamed.influences.some(edge => edge.source === "city" && edge.target === "设计工程师"));
  assert.equal(scene.entities.find(a => a.id === "engineer").id,"engineer");
  assert.throws(() => editor.renameActor(scene,"engineer","city"),/已存在/);
});

test("containment cycles are rejected while influence feedback remains valid", () => {
  const scene=demo();
  assert.equal(editor.depths(scene).engineer,2);
  scene.entities.find(a=>a.id==="city").subsystem_id="port_team";
  assert.throws(()=>editor.depths(scene),/包含循环/);
});

test("domain rename moves every field and material without changing missing values or ACLs", () => {
  const scene=demo(),old=scene.domains[0];
  scene.entities[0].fields[0].value=null;
  const next=editor.renameDomain(scene,old,"新的自定义领域");
  for (const actor of next.entities) for (const item of [...actor.fields,...actor.materials]) assert.notEqual(item.domain,old);
  assert.equal(next.entities[0].fields[0].value,null);
  assert.deepEqual(next.entities[0].fields[0].visibility,scene.entities[0].fields[0].visibility);
});

test("readiness separates missing weights and unconfigured layers from assumptions", () => {
  const scene=demo();scene.influences[0].weight=null;
  const result=editor.checks(scene,{models:{layers:{0:"same",1:"same"}}});
  assert(result.some(item=>item.kind==="error"&&item.step==="relations"));
  assert(result.some(item=>item.kind==="error"&&item.text.includes("L2")));
  assert(result.some(item=>item.kind==="warning"&&item.step==="review"));
});
