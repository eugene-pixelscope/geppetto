import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const code=await readFile(new URL('../web/charts.js',import.meta.url),'utf8');
const {smoothValues}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
const original=[0,10,0];assert.deepEqual(smoothValues(original,0),original);assert.deepEqual(original,[0,10,0]);
const smoothed=smoothValues(original,.5);assert.ok(Math.abs(smoothed[1]-20/3)<1e-10);assert.ok(Math.abs(smoothed[2]-20/7)<1e-10);
for(const value of smoothValues([5,5,5,5],.99))assert.ok(Math.abs(value-5)<1e-10);
assert.deepEqual(smoothValues([],0.6),[]);assert.deepEqual(smoothValues([2,NaN,2],.6),[2,null,2]);
console.log('PASS: zero smoothing, bias correction, constant signal, gaps, original immutability');
