import json, re, numpy as np, soundfile as sf, sys
from kokoro_onnx import Kokoro
V = sys.argv[1]; OUT = sys.argv[2]
k = Kokoro(V+"/kokoro.onnx", V+"/voices.bin")
SEG = [
 ("intro","TOP STORIES","Tuesday, November 17, 2026",
  ["This is your breaking news update for Tuesday, November seventeenth, twenty twenty-six.","Here are today's top headlines."]),
 ("iran","MIDDLE EAST","Iran says it struck U.S. radar in Bahrain",
  ["Tensions with Iran are escalating.","Iran's Revolutionary Guard says it struck a U.S. radar system at Bahrain's Sheikh Isa air base.","A third American aircraft carrier has now arrived in the Gulf, and Qatar is calling for restraint."]),
 ("dc","NATIONAL SECURITY","White House: no credible threat to D.C.",
  ["Reports say U.S. intelligence is assessing possible Iranian plots targeting Washington, D.C.","The White House says there is no credible threat at this time.","President Trump says the U.S. takes any possible threat very seriously."]),
 ("asia","WORLD","Trump in Philippines; possible Kim meeting",
  ["In Asia, President Trump is visiting the Philippines for trade talks.","He also says he could potentially meet North Korean leader Kim Jong Un in China, on the sidelines of the APEC summit."]),
 ("outage","DEVELOPING","Outages hit major banks and United Airlines",
  ["Back home, customers are reporting outages at major banks, while United Airlines reports widespread outages.","These are early reports, and details may change."]),
 ("korea","EDUCATION","South Korea targets AI cheating before CSAT",
  ["In South Korea, with the national college entrance exam just two days away, officials are set to unveil new measures against A.I.-assisted cheating."]),
 ("bond","ENTERTAINMENT","Robert Pattinson named the next James Bond",
  ["And in entertainment, Amazon MGM Studios has confirmed that Robert Pattinson is the next James Bond."]),
 ("outro","STAY TUNED","More as these stories develop",
  ["That's your update. Stay tuned for more as these stories develop."]),
]
# display text for captions (spell-outs replaced)
DISP = {"November seventeenth, twenty twenty-six":"November 17th, 2026","A.I.-assisted":"AI-assisted"}
SR=24000; t=1.2; audio=[np.zeros(int(SR*t))]; segs=[]; caps=[]
for key,tag,head,sents in SEG:
    st=t
    for s in sents:
        a,sr=k.create(s,voice="am_michael",speed=1.02,lang="en-us"); assert sr==SR
        d=len(a)/SR; disp=s
        for x,y in DISP.items(): disp=disp.replace(x,y)
        words=disp.split(); chunks=[]; cur=[]
        for w in words:
            cur.append(w)
            if len(" ".join(cur))>38 or w.endswith((",",".")) and len(cur)>=4: chunks.append(" ".join(cur)); cur=[]
        if cur: chunks.append(" ".join(cur))
        tot=sum(len(c)+3 for c in chunks); ct=t
        for c in chunks:
            cd=d*(len(c)+3)/tot; caps.append((ct,ct+cd,c)); ct+=cd
        audio.append(a); t+=d
        gap=0.35; audio.append(np.zeros(int(SR*gap))); t+=gap
    gap=0.6; audio.append(np.zeros(int(SR*gap))); t+=gap
    segs.append(dict(key=key,tag=tag,head=head,start=st,end=t))
audio.append(np.zeros(int(SR*2.0))); t+=2.0
sf.write(OUT+"/voice.wav",np.concatenate(audio),SR)
json.dump(dict(dur=t,segs=segs,caps=caps),open(OUT+"/timeline.json","w"),indent=1)
print("duration",t)
