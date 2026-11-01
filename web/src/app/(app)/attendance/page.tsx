"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, ErrorText, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Table, type Tone } from "@/components/ui";
import { api, ensureOk, rawFetch, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { can } from "@/lib/permissions";
import type { MessageKey } from "@/lib/messages/en";
import { useShifts, useStaff } from "@/lib/queries";
import { localDate, zonedToUtc } from "@/lib/time";

type Day = Schemas["AttendanceOut"];
type Status = Day["status"];
type Staff = Schemas["StaffOut"];

const STATUSES: Status[] = ["on_time", "late", "absent", "day_off"];
const TONE: Record<Status, Tone> = { on_time: "green", late: "amber", absent: "red", day_off: "slate" };

function shiftDate(date: string, days: number): string {
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/** "HH:MM" in the branch's time zone, with "+1" when it falls on the next day. */
function useClock(timeZone: string) {
  const { locale } = useI18n();
  const format = new Intl.DateTimeFormat(intlLocale(locale), { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  return (iso: string | null | undefined, workDate: string): string => {
    if (!iso) return "—";
    const instant = new Date(iso);
    return localDate(instant, timeZone) > workDate ? `${format.format(instant)} (+1)` : format.format(instant);
  };
}

type Form = { status: Status; checkIn: string; checkOut: string; note: string };

function CorrectDialog({ staff, day, workDate, timeZone, onClose }: { staff: Staff | null; day: Day | undefined; workDate: string; timeZone: string; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const hhmm = (iso: string | null | undefined) =>
    iso ? new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(iso)) : "";
  const [form, setForm] = useState<Form>(() => ({
    status: day?.status ?? "on_time",
    checkIn: hhmm(day?.check_in_at),
    checkOut: hhmm(day?.check_out_at),
    note: day?.note ?? "",
  }));
  const present = form.status === "on_time" || form.status === "late";

  const save = useMutation({
    mutationFn: () => {
      let checkIn: string | null = null;
      let checkOut: string | null = null;
      if (present) {
        checkIn = zonedToUtc(workDate, form.checkIn, timeZone).toISOString();
        if (form.checkOut) {
          // Earlier than the check-in: the shift ended the next morning.
          const outDate = form.checkOut < form.checkIn ? shiftDate(workDate, 1) : workDate;
          checkOut = zonedToUtc(outDate, form.checkOut, timeZone).toISOString();
        }
      }
      return unwrap(
        api.PUT("/v1/attendance/{staff_id}/{work_date}", {
          params: { path: { staff_id: staff!.id, work_date: workDate } },
          body: { status: form.status, check_in_at: checkIn, check_out_at: checkOut, note: form.note.trim() || null },
        }),
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["attendance"] });
      onClose();
    },
  });

  return (
    <Modal open={!!staff} onClose={onClose} title={t("attendance.correctTitle", { name: staff?.full_name ?? "" })}>
      <form
        className="grid grid-cols-2 gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label={t("common.status")} className="col-span-2">
          {(id) => (
            <Select id={id} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value as Status })}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`attendance.status.${s}` as MessageKey)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        {present && (
          <>
            <Field label={t("attendance.checkIn")}>
              {(id) => <Input id={id} type="time" required value={form.checkIn} onChange={(e) => setForm({ ...form, checkIn: e.target.value })} />}
            </Field>
            <Field label={t("attendance.checkOut")} hint={t("common.optional")}>
              {(id) => <Input id={id} type="time" value={form.checkOut} onChange={(e) => setForm({ ...form, checkOut: e.target.value })} />}
            </Field>
            <p className="col-span-2 text-xs text-slate-500">{t("attendance.nextDayHint")}</p>
          </>
        )}
        <Field label={t("attendance.note")} className="col-span-2">
          {(id) => <Input id={id} maxLength={500} value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />}
        </Field>
        <div className="col-span-2">
          <ErrorText error={save.error} />
        </div>
        <Button type="submit" variant="primary" className="col-span-2" disabled={save.isPending}>
          {t("common.save")}
        </Button>
      </form>
    </Modal>
  );
}

function AttendanceDay({ branchId }: { branchId: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const { timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const timeZone = timezoneOf(branchId);
  const today = localDate(new Date(), timeZone);
  const [date, setDate] = useState(today);
  const [editing, setEditing] = useState<Staff | null>(null);
  const clock = useClock(timeZone);

  const staff = useStaff(branchId);
  const shifts = useShifts(branchId);
  const days = useQuery({
    queryKey: ["attendance", tenantId, branchId, date],
    queryFn: () => unwrap(api.GET("/v1/attendance", { params: { query: { branch_id: branchId, date_from: date, date_to: date } } })),
    enabled: !!tenantId,
  });
  const undo = useMutation({
    mutationFn: (staffId: string) =>
      unwrap(api.DELETE("/v1/attendance/{staff_id}/{work_date}", { params: { path: { staff_id: staffId, work_date: date } } })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["attendance"] }),
  });

  const byStaff = new Map((days.data ?? []).map((d) => [d.staff_id, d]));
  // Active staff, plus anyone who has a record that day (e.g. left since).
  const people = (staff.data ?? []).filter((s) => s.status === "active" || byStaff.has(s.id));
  const shiftName = (id: string | null) => shifts.data?.find((s) => s.id === id)?.name ?? t("staff.noShift");
  const counts = { on_time: 0, late: 0, absent: 0, not_seen: 0 };
  for (const person of people) {
    const status = byStaff.get(person.id)?.status;
    if (!status) counts.not_seen += 1;
    else if (status !== "day_off") counts[status] += 1;
  }

  return (
    <>
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <Button onClick={() => setDate(shiftDate(date, -1))} aria-label={t("attendance.prevDay")}>
          <span className="rtl:rotate-180">‹</span>
        </Button>
        <Field label={t("common.date")}>
          {(id) => <Input id={id} type="date" max={today} value={date} onChange={(e) => e.target.value && setDate(e.target.value)} />}
        </Field>
        <Button onClick={() => setDate(shiftDate(date, 1))} disabled={date >= today} aria-label={t("attendance.nextDay")}>
          <span className="rtl:rotate-180">›</span>
        </Button>
        <Button variant="ghost" onClick={() => setDate(today)} disabled={date === today}>
          {t("attendance.today")}
        </Button>
        <p className="ms-auto text-sm text-slate-600">{t("attendance.summary", counts)}</p>
      </div>
      <Card>
        {staff.isLoading || days.isLoading ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("attendance.staff")}</th>
                <th>{t("staff.shift")}</th>
                <th>{t("common.status")}</th>
                <th>{t("attendance.checkIn")}</th>
                <th>{t("attendance.checkOut")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {people.map((person) => {
                const day = byStaff.get(person.id);
                const manual = day?.source === "manual";
                return (
                  <tr key={person.id}>
                    <td>
                      <p className="font-medium">{person.full_name}</p>
                      <p className="text-xs text-slate-500">{person.employee_code}</p>
                    </td>
                    <td>{shiftName(person.shift_id)}</td>
                    <td>
                      {day ? (
                        <Badge tone={TONE[day.status]}>{t(`attendance.status.${day.status}` as MessageKey)}</Badge>
                      ) : (
                        <Badge>{t("attendance.notSeen")}</Badge>
                      )}
                      {manual && (
                        <p className="mt-1 text-xs text-slate-500" title={day.note ?? undefined}>
                          {t("attendance.corrected", { name: day.corrected_by ?? "" })}
                          {day.note && ` · ${day.note}`}
                        </p>
                      )}
                    </td>
                    <td dir="ltr" className="text-start">
                      {clock(day?.check_in_at, date)}
                    </td>
                    <td dir="ltr" className="text-start">
                      {clock(day?.check_out_at, date)}
                      {manual && day.first_seen_at && (
                        <p className="text-xs text-slate-500" dir="auto">
                          {t("attendance.detected", {
                            times:
                              day.last_seen_at && day.last_seen_at !== day.first_seen_at
                                ? `${clock(day.first_seen_at, date)}–${clock(day.last_seen_at, date)}`
                                : clock(day.first_seen_at, date),
                          })}
                        </p>
                      )}
                    </td>
                    <td className="text-end whitespace-nowrap">
                      <Button size="sm" variant="ghost" onClick={() => setEditing(person)}>
                        {t("attendance.correct")}
                      </Button>
                      {manual && (
                        <Button size="sm" variant="ghost" onClick={() => window.confirm(t("attendance.removeConfirm")) && undo.mutate(person.id)}>
                          {t("attendance.removeCorrection")}
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
        <ErrorText error={staff.error ?? days.error ?? undo.error} />
      </Card>
      {editing && (
        <CorrectDialog
          key={`${editing.id}-${date}`}
          staff={editing}
          day={byStaff.get(editing.id)}
          workDate={date}
          timeZone={timeZone}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  );
}

function AttendanceContent() {
  const { t } = useI18n();
  const { branchId } = useBranches();
  if (!branchId) return <Notice>{t("attendance.chooseBranch")}</Notice>;
  return <AttendanceDay key={branchId} branchId={branchId} />;
}

/** A month of attendance as CSV for the payroll system (owners; audited). The payroll
 * system can also fetch it itself with an API key (Administration › Integrations). */
function PayrollExport() {
  const { t } = useI18n();
  const { branchId } = useBranches();
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function download() {
    setBusy(true);
    setError(null);
    try {
      const [y, m] = month.split("-").map(Number) as [number, number];
      const last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
      const params = new URLSearchParams({ from: `${month}-01`, to: last, format: "csv" });
      if (branchId) params.set("branch_id", branchId);
      const response = await ensureOk(await rawFetch(`/v1/payroll/attendance?${params}`));
      const url = URL.createObjectURL(await response.blob());
      const link = Object.assign(document.createElement("a"), { href: url, download: `attendance-${month}.csv` });
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        <Input type="month" aria-label={t("attendance.payrollMonth")} value={month} onChange={(e) => setMonth(e.target.value)} className="w-auto" />
        <Button onClick={() => void download()} disabled={busy || !month}>
          {t("attendance.payrollExport")}
        </Button>
      </div>
      <ErrorText error={error} />
    </div>
  );
}

export default function AttendancePage() {
  const { t } = useI18n();
  const { me } = useAuth();
  return (
    <RequirePermission anyOf={["staff_manage"]}>
      <PageHeader title={t("attendance.title")}>{can(me, "payroll_export") && <PayrollExport />}</PageHeader>
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("attendance.help")}</p>
      <AttendanceContent />
    </RequirePermission>
  );
}
