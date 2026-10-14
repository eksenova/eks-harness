const RealDate: DateConstructor = (globalThis as any).__eksRealDate ?? Date;
let offset = 0;

function shiftedNow(): number {
  return RealDate.now() + offset;
}

function ShiftedDate(this: unknown, ...args: unknown[]): unknown {
  if (!new.target) return new RealDate(shiftedNow()).toString();
  return Reflect.construct(RealDate, args.length ? args : [shiftedNow()], new.target);
}

ShiftedDate.prototype = RealDate.prototype;
Object.setPrototypeOf(ShiftedDate, RealDate);
(ShiftedDate as any).now = shiftedNow;

export function installClock(now: string): { now: string; offset: number } {
  const target = RealDate.parse(now);
  if (Number.isNaN(target)) throw new Error(`clock: "${now}" is not an ISO date`);
  offset = target - RealDate.now();
  (globalThis as any).__eksRealDate = RealDate;
  (globalThis as any).Date = ShiftedDate;
  return { now: new RealDate(shiftedNow()).toISOString(), offset };
}

export function clockCommand(args: any): unknown {
  if (typeof args?.now !== "string") throw new Error("clock needs { now: <ISO date> }");
  return installClock(args.now);
}
