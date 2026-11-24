import assert from "node:assert/strict";
import test from "node:test";
import {sampleTokens} from "../web/reference-data.js";
import {createReferencePicker, mostUsedModels, referenceCaption, tokenBand} from "../web/references.js";

function memoryStorage() {
  const items = new Map();
  return {getItem: key => items.get(key), setItem: (key, value) => items.set(key, value), items};
}

test("natural band thresholds are inclusive", () => {
  const starts = [1, 30, 500, 5000, 50000, 500000, 5000000, 80000000, 2000000000];
  starts.forEach((floor, i) => {
    assert.equal(tokenBand(floor - 1)?.floor, starts[i - 1]);
    assert.equal(tokenBand(floor).floor, floor);
    assert.equal(tokenBand(floor + 1).floor, floor);
  });
  assert.equal(tokenBand(1e14).floor, starts.at(-1));
});

test("unknown, empty, zero and invalid totals have no reference", () => {
  const choose = createReferencePicker(null);
  for (const total of [null, undefined, 0, -1, NaN, Infinity, "100"]) {
    assert.equal(tokenBand(total), null);
    assert.equal(choose("all", total), null);
  }
});

test("every default pool cycles distinct units with one explicit multiplier", () => {
  for (const total of [1,30,500,5000,50000,500000,5000000,80000000,2000000000,1e14]) {
    const choose = createReferencePicker(null, () => .42);
    const pool = tokenBand(total).items;
    const seen = new Set();
    for (let i = 0; i < pool.length; i++) {
      const choice = choose("scope " + i, total);
      assert.ok(pool.includes(choice.item));
      assert.equal(choice.equivalents, total / choice.item.tokens);
      assert.ok(Number.isSafeInteger(choice.capacity) && choice.capacity > 0);
      assert.ok(!seen.has(choice.item.name));
      seen.add(choice.item.name);
      assert.match(referenceCaption(choice), /^约合 /);
      assert.match(referenceCaption(choice,true), /^已知用量约合 /);
    }
  }
});

test("top two bands sometimes pick a fifth-band work and preserve it on redraw", () => {
  for (const total of [80_000_000, 2_000_000_000, 14_600_000_000]) {
    for (const [roll, pool] of [[.299999,tokenBand(500000)], [.30,tokenBand(total)]]) {
      const choose = createReferencePicker(null, () => roll);
      const first = choose("home", total);
      assert.ok(pool.items.includes(first.item));
      assert.equal(choose("home",total+1).item, first.item);
    }
  }
});

test("a deck cycles without replacement and reload advances past the visible item", () => {
  const storage = memoryStorage(), seen = new Set();
  const size = tokenBand(130).items.length;
  let last;
  for (let i = 0; i < size * 3; i++) {
    if (i % size === 0) seen.clear();
    const choose = createReferencePicker(storage, () => .53);
    const pick = choose("private scope name", 130).item;
    assert.notEqual(pick, last);
    assert.ok(!seen.has(pick));
    seen.add(pick);
    last = pick;
  }
  for (const [key, value] of storage.items) {
    assert.ok(!key.includes("private") && !value.includes("private"));
    assert.deepEqual(Object.keys(JSON.parse(value)).sort(), ["last", "left"]);
  }
});

test("sorting, revisiting a scope, and updating its total preserve the same in-band unit", () => {
  const choose = createReferencePicker(null, () => .04);
  const pick = choose("all", 201);
  choose("model A", 109);
  const back = choose("all", 202);
  assert.equal(back.item, pick.item);
  assert.equal(back.equivalents, 202 / pick.capacity);
  assert.notEqual(choose("all", 1000).item, back.item);
  assert.equal(choose("all", null), null);
  assert.equal(referenceCaption(null), "");
});

test("restricted or invalid browser storage falls back to an in-memory deck", () => {
  for (const saved of ["{", "null", '{"left":[20],"last":1}', '{"left":[0,0],"last":1}']) {
    const choose = createReferencePicker({getItem:() => saved, setItem:() => {throw Error("blocked");}});
    const first = choose("all", 45).item;
    assert.notEqual(choose("model A", 45).item, first);
  }
});

test("public edition counts match the calibrated sources", () => {
  for (const [key, expected] of Object.entries({tao:7514, crime:283012, red:898593})) {
    assert.equal(sampleTokens[key], expected);
  }
});

test("model frequency uses record counts, retains ties and excludes unidentified records", () => {
  const m = (model, n, total, vendor="Fictional") => ({model, n, total, vendor});
  const a = m("Alpha", 3, 8), b = m("Beta", 3, null), c = m("Large", 1, 1000);
  const unknown = [m("AGY #42 · 型号未确认",15,120), m("Codex · 型号未记录",9,null), m("型号未记录",2,21)];
  assert.deepEqual(mostUsedModels([b,c,...unknown,a]), new Set([a,b]));
  assert.deepEqual(mostUsedModels([c,...unknown]), new Set([c]));
  assert.deepEqual(mostUsedModels(unknown), new Set());
});
