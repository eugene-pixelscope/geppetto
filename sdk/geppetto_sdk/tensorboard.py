"""Optional bridge. Install tensorboard in the caller's training environment."""

def import_events(run, logdir):
    """Import TensorBoard scalars and raw histograms with their original steps.

    Use a fresh Geppetto run: scalar steps must remain monotonic. Existing event
    timestamps are not rewritten as training elapsed times by this bridge.
    """
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    accumulator=EventAccumulator(str(logdir),size_guidance={'scalars':0,'histograms':0})
    accumulator.Reload()
    steps={}
    for key in accumulator.Tags().get('scalars',[]):
        for event in accumulator.Scalars(key):steps.setdefault(event.step,{})[key]=event.value
    for step,metrics in sorted(steps.items()):run.log(metrics,step=step)
    for key in accumulator.Tags().get('histograms',[]):
        for event in accumulator.Histograms(key):
            h=event.histogram_value
            # TB has infinite boundary sentinels. Keep finite bounds enclosing all mass.
            import math
            counts=list(h.bucket);upper=list(h.bucket_limit)
            if not counts:continue
            lo=h.min;hi=h.max
            if lo==hi:lo-=.5;hi+=.5
            pairs=[(min(hi,max(lo,x)),v) for x,v in zip(upper,counts)]
            edges=[lo];bins=[];pending=0
            for edge,count in pairs:
                pending+=count
                if math.isfinite(edge) and edge>edges[-1]:
                    edges.append(edge);bins.append(pending);pending=0
            if pending and bins:bins[-1]+=pending
            if not bins:continue
            if len(bins)>256:
                raise ValueError('TensorBoard histogram exceeds 256 bins; rebin before importing')
            run._client.post('runs/'+run.id+'/visualizations',dict(type='histogram',name=key,step=event.step,data=dict(edges=edges,counts=bins)))


def import_chrome_trace(run, path, *, name='profiler', step=0):
    """Import PyTorch/Chrome complete-duration events (microseconds)."""
    import json
    from pathlib import Path
    raw=json.loads(Path(path).read_text())
    source=raw if isinstance(raw,list) else raw['traceEvents']
    events=[{'name':e.get('name','event'),'lane':str(e.get('pid',0))+':'+str(e.get('tid',0)),
             'start_us':e['ts'],'duration_us':e['dur']} for e in source if e.get('ph')=='X' and 'ts' in e and 'dur' in e]
    return run.log_trace(name,events,step=step)
