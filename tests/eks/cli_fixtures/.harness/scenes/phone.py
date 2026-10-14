import eks
from eks import t


@eks.scene(kind="blender", engine="cycles", samples=32, seed=7)
def phone(sc: eks.BlenderScene, screen: eks.Media):
    p = sc.load("assets/phone.blend", obj="Phone")
    p.material("Screen").texture("screen", screen)
    cam = sc.camera(lens=50).look_at(p)
    cam.animate("location", {t("bar:0"): (0, -4, 1), t("bar:4"): (0, -2, 1)}, ease="out_cubic")
    sc.on(t.beats("downbeat")[0:8], p.pulse(scale=1.04))

    @sc.bpy
    def rig(bpy, ctx):
        return None
