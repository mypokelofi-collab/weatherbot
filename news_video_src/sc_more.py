from kit import *
X0,Y0=PAD,PAD
class Outage:
    """airport terminal: departure board flipping to DELAYED, ATM out of service, travelers"""
    ROWS=[("08:15","CHICAGO","UA 512"),("08:40","DENVER","UA 1189"),("09:05","NEWARK","UA 233"),("09:20","HOUSTON","UA 708"),("09:45","SAN FRANCISCO","UA 92"),("10:10","WASHINGTON","UA 377")]
    def __init__(s):
        fy=Y0+540; s.fy=fy
        s.wall=full(lambda d:d.rectangle([0,0,CW,CH]),(200,195,205),(160,155,170),701,0.05,shadow=False,rough_amt=0,mottle=0.06)
        # big windows with dusk sky + plane silhouette outside
        def win(d):
            for x in range(-200,CW,330): d.rectangle([x,Y0+40,x+300,fy-120])
        s.win=full(win,(120,170,220),(250,190,150),702,0.1,shadow=False,rough_amt=0.5)
        def plane(d):
            d.rounded_rectangle([X0+200,fy-260,X0+900,fy-190],30); d.polygon([(X0+200,fy-250),(X0+260,fy-250),(X0+230,fy-360),(X0+190,fy-360)]); d.polygon([(X0+450,fy-210),(X0+620,fy-210),(X0+520,fy-150),(X0+480,fy-150)])
        s.op=full(plane,(90,95,120),(70,72,95),703,0.12,shadow=False,rough_amt=1)
        def mull(d):
            for x in range(-200,CW,330): d.rectangle([x-12,Y0+20,x+4,fy-100])
            d.rectangle([0,Y0+20,CW,Y0+44]); d.rectangle([0,fy-124,CW,fy-100])
        s.mull=full(mull,(110,110,125),(80,80,95),704,0.16,op=0.4,rough_amt=0)
        s.floor=full(lambda d:d.rectangle([0,fy-100,CW,CH]),(185,175,165),(120,110,105),705,0.3,ang=0)
        detail(s.floor.img,lambda d:[d.line([0,y,CW,y],fill=(150,140,135,255),width=2) for y in range(fy-60,CH,45)],1)
        # board
        s.bw,s.bh=620,340
        def board(d): d.rounded_rectangle([0,0,620,340],14)
        s.board=sprimg(board,(620,340),(40,42,55),(25,26,35),706,2,0.6,0.08)
        s.f=font(40,True); s.fh=font(40,True)
        # ATM
        def atm(d): d.rounded_rectangle([0,0,170,330],12); d.rectangle([-6,-20,176,20])
        s.atm=sprimg(atm,(170,330),(120,130,150),(80,85,100),707,2,0.6,0.1)
        detail(s.atm,lambda d:[d.rectangle([20,60,150,150],fill=(30,40,60,255)),d.rectangle([40,180,130,240],fill=(60,65,80,255)),d.rectangle([40,260,130,270],fill=(30,30,40,255))])
        s.people=[person_img(int(h),720+i,col=c) for i,(h,c) in enumerate([(150,((60,70,110),(40,45,80))),(140,((110,60,70),(70,40,50))),(160,((50,90,80),(30,60,55))),(145,((90,80,60),(60,50,40))),(155,((70,60,100),(45,40,70)))])]
        s.case=sprimg(lambda d:[d.rounded_rectangle([0,10,40,60],6),d.rectangle([16,0,24,12])],(40,60),(60,60,75),(30,30,40),730,2,0.5)
        s.pillar=full(lambda d:[d.rectangle([X0-120,0,X0-30,CH]),d.rectangle([X0+1300,0,X0+1390,CH])],(170,165,175),(120,115,130),731,0.85,op=0.5,rough_amt=1)
        s.glow=glow(120,(255,200,90,90))
    def board_img(s,t):
        im=s.board.copy(); d=ImageDraw.Draw(im); k=2
        d.text((50,40),"DEPARTURES",font=font(56),fill=(255,210,80,255))
        d.text((960,56),"STATUS",font=s.fh,fill=(200,200,210,255))
        for i,(tm,city,fl) in enumerate(s.ROWS):
            y=(70+i*42)*2
            t0=1.0+i*0.7
            st="ON TIME" if t<t0 else ("CANCELLED" if i in (2,4) else "DELAYED")
            col=(120,230,140,255) if st=="ON TIME" else ((255,80,70,255) if st=="CANCELLED" else (255,190,60,255))
            d.text((50,y),tm,font=s.f,fill=(240,240,240,255)); d.text((220,y),city,font=s.f,fill=(240,240,240,255)); d.text((720,y),fl,font=s.f,fill=(200,200,210,255))
            # split-flap flip animation on the status cell
            u=(t-t0)/0.35
            cell=Image.new("RGBA",(250,60),(18,18,24,255)); cd=ImageDraw.Draw(cell); cd.text((8,8),st,font=s.f,fill=col)
            cd.line([0,30,250,30],fill=(0,0,0,255),width=2)
            if 0<u<1: cell=cell.resize((250,max(2,int(60*abs(math.cos(u*math.pi))))))
            im.alpha_composite(cell,(960,y-6+(60-cell.height)//2))
        return im
    def atm_img(s,t):
        im=s.atm.copy(); d=ImageDraw.Draw(im)
        on=(t*3)%1<0.8 if t>1.5 else True
        if t<1.5: d.text((170,210),"WELCOME",font=font(26),fill=(140,220,255,255),anchor="mm")
        elif on:
            d.rectangle([40,120,300,300],fill=(120,20,25,255)); d.text((170,180),"OUT OF",font=font(28),fill=(255,230,230,255),anchor="mm"); d.text((170,230),"SERVICE",font=font(28),fill=(255,230,230,255),anchor="mm")
        return im
    def items(s,t):
        fy=s.fy; L=tile(s.wall)+[(s.win,0,0),(s.op,0,0),(s.mull,0,0),(s.floor,0,0)]
        L.append((Layer(fade(s.glow,0.8),0.31,shadow=False),X0+430-120+310,Y0+120))
        L.append((dyn(s.board_img(t),0.32,sdist=14,blur=8),X0+330,Y0+70))
        L.append((dyn(s.atm_img(t),0.4,sdist=10),X0+1050,fy-260))
        # travelers: some walk, some stand looking at the board
        for i,p in enumerate(s.people):
            if i<2: x=X0+380+i*160+4*math.sin(t+i); y=fy-100
            else: x=X0-200+((t*(55+i*12)+i*500)%1800); y=fy-80+i*12
            pp=p if i<2 else person_img(p.height//2-1,720+i,pose=t*6+i) if False else p
            L.append((dyn(pp,0.45+i*0.03,sdist=8),x,y))
            if i>=2: L.append((dyn(s.case,0.45+i*0.03,sdist=6),x+60,y+92))
        L.append((s.pillar,t*0,0))
        return L
    def frame(s,t,dur):
        L=s.items(t); fy=s.fy
        if t<3.8:
            u=t/3.8; cam=Cam(mix(X0+560,X0+700,u),fy-200,zoom=mix(1.0,1.2,ease(u)),fd=0.35,panx=mix(-0.2,0.2,u),dolly=0.9,dof=0.6)
        elif t<8.0:
            u=(t-3.8)/4.2; cam=Cam(X0+640+mix(-15,15,u),Y0+70+170,zoom=mix(1.75,1.95,ease(u)),fd=0.32,panx=mix(0.08,-0.08,u),dolly=1.2,dof=1.2)
        else:
            u=(t-8.0)/(dur-8.0); cam=Cam(X0+1135,fy-160,zoom=mix(2.6,2.2,ease(u)),fd=0.4,panx=mix(-0.1,0.1,u),pany=0.03,dolly=1.1,dof=1.3)
        tt=t if t<3.8 else (t-3.8+0.4 if t<8 else t)
        return shoot(L,cam,bgimg=BG_T)
BG_T=screen_grad((200,195,205),(160,155,170))

class Exam:
    def __init__(s):
        fy=Y0+560; s.fy=fy
        s.wall=full(lambda d:d.rectangle([0,0,CW,CH]),(235,225,200),(210,195,170),801,0.05,shadow=False,rough_amt=0,mottle=0.06)
        def wins(d):
            for x in range(X0-500,CW,300): d.rectangle([x,Y0+60,x+200,Y0+330])
        s.win=full(wins,(200,230,250),(170,210,240),802,0.08,shadow=False,rough_amt=0.5)
        s.board=full(lambda d:d.rectangle([X0+330,Y0+80,X0+950,Y0+330]),(50,75,60),(35,55,45),803,0.12,op=0.4,rough_amt=1)
        dd=ImageDraw.Draw(s.board.img); dd.text((X0+640,Y0+200),"수학능력시험",font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",10),fill=(0,0,0,0))
        for i,txt in enumerate(("COLLEGE ENTRANCE EXAM","NO PHONES  ·  NO DEVICES","EYES ON YOUR OWN PAPER")):
            dd.text((X0+640,Y0+140+i*60),txt,font=font(34 if i==0 else 26),fill=(240,240,230,235),anchor="mm")
        s.floor=full(lambda d:d.rectangle([0,fy-120,CW,CH]),(170,130,95),(120,90,65),804,0.2,ang=0)
        s.beam=Image.new("RGBA",(CW,CH)); bd=ImageDraw.Draw(s.beam)
        for x in range(X0-500,CW,300): bd.polygon([(x,Y0+60),(x+200,Y0+60),(x+600,CH),(x+300,CH)],fill=(255,250,220,22))
        s.beamL=Layer(s.beam.filter(ImageFilter.GaussianBlur(12)),0.5,shadow=False)
        def desk(d): d.rectangle([0,0,150,14]); d.rectangle([10,14,20,80]); d.rectangle([130,14,140,80])
        s.desk=sprimg(desk,(150,82),(200,160,110),(150,115,80),805,2,0.5)
        s.stud=[sprimg(lambda d,i=i:[d.ellipse([30,0,80,52]),d.rounded_rectangle([10,48,100,140],18)],(110,142),c[0],c[1],810+i,2,0.8) for i,c in enumerate([((40,50,90),(25,30,60)),((60,60,70),(35,35,45)),((40,50,90),(25,30,60))])]
        s.arm=sprimg(lambda d:d.rounded_rectangle([0,0,60,18],8),(60,18),(40,50,90),(25,30,60),820,2,0.5)
        s.clock=sprimg(lambda d:d.ellipse([0,0,120,120]),(120,120),(250,250,245),(225,225,215),830,2,0.5)
        detail(s.clock,lambda d:[d.ellipse([0,0,120,120],outline=(60,40,40,255),width=6)]+[d.line([60+50*math.cos(a),60+50*math.sin(a),60+42*math.cos(a),60+42*math.sin(a)],fill=(40,40,40,255),width=3) for a in [i*math.pi/6 for i in range(12)]])
        s.proctor=person_img(190,840,col=((80,80,95),(50,50,65)))
        s.paper=sprimg(lambda d:d.rectangle([0,0,300,200]),(300,200),(252,250,244),(235,232,225),850,2,0.4,0.06)
        detail(s.paper,lambda d:[d.text((14,10),"ANSWER SHEET",font=font(15),fill=(60,60,90,255))]+[d.ellipse([20+c*40,44+r*30,36+c*40,60+r*30],outline=(150,150,170,255),width=1) for r in range(5) for c in range(6)])
        s.pencil=sprimg(lambda d:d.polygon([(0,0),(170,0),(195,9),(170,18),(0,18)]),(196,18),(255,200,40),(230,160,20),860,2,0.4)
        detail(s.pencil,lambda d:[d.polygon([(170,0),(195,9),(170,18)],fill=(230,200,160,255)),d.polygon([(186,5),(195,9),(186,13)],fill=(40,40,40,255)),d.rectangle([0,0,18,18],fill=(240,140,150,255))])
        s.hand=sprimg(lambda d:[d.ellipse([0,0,70,50]),d.rounded_rectangle([40,10,160,44],16)],(160,50),(240,200,170),(210,165,135),870,2,0.5)
    def items(s,t):
        fy=s.fy; L=tile(s.wall)+[(s.win,0,0),(s.board,0,0)]
        c=s.clock.copy(); d=ImageDraw.Draw(c); a=-math.pi/2+t*2*math.pi/60*6; ah=-math.pi/2+2.6
        d.line([120,120,120+75*math.cos(ah),120+75*math.sin(ah)],fill=(30,30,30,255),width=8)
        d.line([120,120,120+95*math.cos(-math.pi/2+1.2+t*0.02),120+95*math.sin(-math.pi/2+1.2+t*0.02)],fill=(30,30,30,255),width=6)
        a=-math.pi/2+int(t*1)*math.pi/30; d.line([120,120,120+100*math.cos(a),120+100*math.sin(a)],fill=(200,30,30,255),width=3)
        L.append((dyn(c,0.13,sdist=6),X0+1060,Y0+100))
        L.append((s.floor,0,0)); L.append((s.beamL,0,0))
        for row in range(4):
            depth=0.3+row*0.12; y=fy-90+row*85; sc=1
            for col in range(7):
                x=X0-200+col*260+row*40
                st=s.stud[(row+col)%3]
                L.append((dyn(st,depth,sdist=6),x+20,y-120))
                ar=s.arm.rotate(8*math.sin(t*6+row*2+col),resample=Image.BICUBIC,expand=True)
                L.append((dyn(ar,depth+0.001,sdist=3),x+90,y-60))
                L.append((dyn(s.desk,depth+0.002,sdist=6),x,y-30))
        px=X0+200+((t*60)%1000); L.append((dyn(s.proctor,0.62,sdist=10),px,fy+40))
        # close-up desk with sheet + writing hand (foreground)
        L.append((dyn(s.paper,0.9,sdist=8),X0+500,fy+240))
        hx=X0+560+60*((t*0.9)%1)+8*math.sin(t*14); hy=fy+300+((t*0.9)//1%4)*30+4*math.cos(t*14)
        L.append((dyn(rot(s.pencil,30),0.91,sdist=10),hx,hy-60))
        L.append((dyn(s.hand,0.92,sdist=10),hx+70,hy-20))
        return L
    def frame(s,t,dur):
        L=s.items(t); fy=s.fy
        if t<4:
            u=t/4; cam=Cam(X0+640,fy-170,zoom=mix(1.0,1.25,ease(u)),fd=0.45,panx=mix(-0.14,0.14,u),pany=0.02,dolly=0.9,dof=0.6)
        elif t<7.5:
            u=(t-4)/3.5; cam=Cam(X0+1120,Y0+160,zoom=mix(2.8,3.2,ease(u)),fd=0.13,panx=mix(0.08,-0.08,u),dolly=1.2,dof=1.4)
        else:
            u=(t-7.5)/(dur-7.5); cam=Cam(X0+660,fy+320,zoom=mix(2.6,2.3,ease(u)),fd=0.9,panx=mix(-0.1,0.08,u),pany=0.06,dolly=1.1,dof=1.4)
        return shoot(L,cam,bgimg=BG_E)
BG_E=screen_grad((235,225,200),(210,195,170))

class Film:
    def __init__(s):
        fy=Y0+560; s.fy=fy
        s.wall=full(lambda d:d.rectangle([0,0,CW,CH]),(25,20,30),(45,35,45),901,0.05,shadow=False,rough_amt=0,mottle=0.08)
        def curtains(d):
            for x in range(0,CW,70): d.polygon([(x,0),(x+70,0),(x+60+10*math.sin(x),fy),(x+8,fy)])
        s.curt=full(curtains,(140,25,40),(80,15,25),902,0.12,op=0.5,rough_amt=1)
        s.floor=full(lambda d:d.rectangle([0,fy-40,CW,CH]),(70,55,50),(35,28,26),903,0.25,ang=0)
        s.pool=glow(260,(255,240,200,150),power=1.5)
        s.beam=beam(900,30,420,(255,245,215),80)
        s.figure=sprimg(lambda d:[person(d,300)],(110,302),(25,25,32),(10,10,15),904,2,0.6)
        def cam(d):
            d.rounded_rectangle([60,90,330,210],14); d.polygon([(330,120),(420,95),(420,205),(330,180)])
            d.ellipse([60,0,150,90]); d.ellipse([180,0,270,90])
            d.polygon([(180,210),(210,210),(120,480),(100,480)]); d.polygon([(200,210),(220,210),(220,480),(200,480)]); d.polygon([(210,210),(240,210),(330,480),(310,480)])
        s.cam=sprimg(cam,(425,482),(60,60,70),(30,30,38),905,2,0.6)
        s.reel=sprimg(lambda d:d.ellipse([0,0,90,90]),(90,90),(80,80,95),(50,50,60),906,2,0.4)
        detail(s.reel,lambda d:[d.ellipse([45-14+20*math.cos(a),45-14+20*math.sin(a),45+14+20*math.cos(a),45+14+20*math.sin(a)],fill=(25,25,30,255)) for a in [i*2*math.pi/5 for i in range(5)]]+[d.ellipse([39,39,51,51],fill=(160,160,170,255))])
        def clap_base(d): d.rectangle([0,0,300,190])
        s.cb=sprimg(clap_base,(300,190),(35,35,40),(20,20,25),907,2,0.5)
        detail(s.cb,lambda d:[d.text((20,20),"PROD.  NEXT 007 FILM",font=font(17),fill=(240,240,240,255)) if False else d.text((20,22),"PRODUCTION",font=font(17),fill=(240,240,240,255)),d.line([0,70,300,70],fill=(220,220,220,255),width=2),d.text((20,82),"SCENE  1",font=font(26),fill=(240,240,240,255)),d.text((170,82),"TAKE  1",font=font(26),fill=(240,240,240,255)),d.line([0,130,300,130],fill=(220,220,220,255),width=2),d.text((20,145),"NOV 17 2026",font=font(18),fill=(240,240,240,255))])
        def arm(d): d.rectangle([0,0,300,40])
        s.ca=sprimg(arm,(300,40),(35,35,40),(20,20,25),908,2,0.5)
        detail(s.ca,lambda d:[d.polygon([(i*60,0),(i*60+30,0),(i*60+50,40),(i*60+20,40)],fill=(245,245,245,255)) for i in range(5)])
        s.chair=sprimg(lambda d:[d.rectangle([0,0,140,70]),d.rectangle([0,110,140,125]),d.line([10,70,130,230],fill=255,width=8),d.line([130,70,10,230],fill=255,width=8)],(140,232),(40,40,45),(20,20,25),909,2,0.5)
        detail(s.chair,lambda d:d.text((70,35),"DIRECTOR",font=font(16),fill=(240,240,240,255),anchor="mm"))
        s.flash=glow(200,(255,255,255,200),power=1.2)
    def items(s,t):
        fy=s.fy; L=tile(s.wall)+[(s.curt,0,0),(s.floor,0,0)]
        L.append((Layer(fade(s.pool,0.8),0.27,shadow=False),X0+640-260,fy-200))
        for bx,ph in ((X0+300,0),(X0+1000,2)):
            a=60+18*math.sin(t*0.9+ph)+(0 if bx<X0+640 else 60)
            b=s.beam.rotate(-a,resample=Image.BICUBIC,expand=True)
            L.append((Layer(b,0.3,shadow=False,cache=False),bx-b.width/2+math.cos(math.radians(a))*450,Y0-60-b.height/2+math.sin(math.radians(a))*450))
        L.append((dyn(s.figure,0.3,sdist=12,op=0.5),X0+640-55,fy-280))
        L.append((dyn(s.chair,0.45,sdist=8),X0+1000,fy-60))
        # camera with spinning reels
        cx,cy=X0+180,fy-180
        L.append((dyn(s.cam,0.6,sdist=12),cx,cy))
        for k,(rx,ry) in enumerate(((60,0),(180,0))):
            r=s.reel.rotate(-t*240,resample=Image.BICUBIC); L.append((dyn(r,0.601,sdist=4),cx+rx,cy+ry))
        # clapperboard: snaps closed at t=4.4
        bx,by=X0+760,fy+60
        snap=4.4; u=t-snap
        ang=28 if u<-0.6 else (28*(-u/0.6) if u<0 else 0)
        if u<-0.6: ang=28-6*math.sin(max(0,t-3)*3)*0
        L.append((dyn(s.cb,0.8,sdist=12),bx,by))
        R=s.ca.width; pad=Image.new("RGBA",(2*R,2*R)); pad.alpha_composite(s.ca,(R,R-s.ca.height))
        arm=pad.rotate(ang,resample=Image.BICUBIC)
        L.append((dyn(arm,0.801,sdist=12),bx-R/2,by-R/2))
        if 0<u<0.25: L.append((Layer(fade(s.flash,1-u/0.25),0.82,shadow=False,cache=False),bx+150-200,by-200))
        return L
    def frame(s,t,dur):
        L=s.items(t); fy=s.fy
        if t<3.0:
            u=t/3; cam=Cam(X0+640,fy-170,zoom=mix(1.0,1.2,ease(u)),fd=0.3,panx=mix(-0.14,0.14,u),dolly=0.9,dof=0.6)
        elif t<5.8:
            u=(t-3)/2.8; cam=Cam(X0+910,fy+120,zoom=mix(2.6,2.9,ease(u)),fd=0.8,panx=mix(0.1,-0.1,u),pany=0.03,dolly=1.2,dof=1.4)
        else:
            u=(t-5.8)/(dur-5.8); cam=Cam(X0+360,fy-130,zoom=mix(2.4,2.0,ease(u)),fd=0.6,panx=mix(-0.1,0.12,u),dolly=1.1,dof=1.3)
        return shoot(L,cam,bgimg=BG_F)
BG_F=screen_grad((25,20,30),(45,35,45))
