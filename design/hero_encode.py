import bpy, os, sys
S=sys.argv[sys.argv.index("--")+1]; fmt=sys.argv[-1]
frames=sorted(f for f in os.listdir(S+"/frames") if f.endswith(".png"))
scn=bpy.context.scene
scn.render.resolution_x=1920; scn.render.resolution_y=1080; scn.render.resolution_percentage=100
scn.render.fps=30; scn.frame_start=1; scn.frame_end=len(frames)
scn.view_settings.view_transform='Standard'
ed=scn.sequence_editor_create()
coll=ed.strips if hasattr(ed,"strips") else ed.sequences
st=coll.new_image("loop", S+"/frames/"+frames[0], channel=1, frame_start=1)
for f in frames[1:]: st.elements.append(f)
print("strip len", st.frame_final_duration)
scn.render.image_settings.media_type='VIDEO' if hasattr(scn.render.image_settings,"media_type") else None
scn.render.image_settings.file_format='FFMPEG'
ff=scn.render.ffmpeg
if fmt=="mp4": ff.format='MPEG4'; ff.codec='H264'; out=S+"/hero-loop.mp4"
else: ff.format='WEBM'; ff.codec='WEBM'; out=S+"/hero-loop.webm"
ff.constant_rate_factor='CUSTOM'
for attr in ("crf","custom_constant_rate_factor"):
    if hasattr(ff,attr): setattr(ff,attr, 26 if fmt=="mp4" else 36); print("set",attr)
ff.ffmpeg_preset='BEST'; ff.gopsize=30; ff.audio_codec='NONE'
scn.render.filepath=out; scn.render.use_file_extension=False
bpy.ops.render.render(animation=True)
print("WROTE", out, os.path.getsize(out))
