"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, ErrorText, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Table } from "@/components/ui";
import { api, ensureOk, rawFetch, unwrap, type Schemas } from "@/lib/api";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { useShifts, useStaff } from "@/lib/queries";
import { localDate, zonedToUtc } from "@/lib/time";

type Staff = Schemas["StaffOut"];
type StaffForm = { id?: string; branch_id: string; employee_code: string; full_name: string; shift_id: string };
type ShiftForm = { id?: string; branch_id: string; name: string; starts_at: string; ends_at: string; grace_minutes: number };

const MAX_PHOTOS = 5;

function ConsentDialog({ staff, onClose }: { staff: Staff | null; onClose: () => void }) {
  const { t } = useI18n();
  const { timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const tz = timezoneOf(staff?.branch_id);
  const [date, setDate] = useState(() => localDate(new Date(), tz));
  const [confirmed, setConfirmed] = useState(false);
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/v1/staff/{staff_id}/consent", {
          params: { path: { staff_id: staff!.id } },
          // Start of the signing day in the branch's time zone: never in the future.
          body: { signed_at: zonedToUtc(date, "00:00", tz).toISOString() },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["staff"] });
      onClose();
    },
  });
  return (
    <Modal open={!!staff} onClose={onClose} title={`${t("staff.recordConsent")} · ${staff?.full_name ?? ""}`}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label={t("staff.consentDate")}>
          {(id) => <Input id={id} type="date" required max={localDate(new Date(), tz)} value={date} onChange={(e) => setDate(e.target.value)} />}
        </Field>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-1" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
          {t("staff.consentConfirm")}
        </label>
        <ErrorText error={save.error} />
        <Button type="submit" variant="primary" disabled={!confirmed || save.isPending}>
          {t("common.save")}
        </Button>
      </form>
    </Modal>
  );
}

function EnrolDialog({ staff, onClose }: { staff: Staff | null; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [files, setFiles] = useState<File[]>([]);
  const [previews, setPreviews] = useState<string[]>([]);

  useEffect(() => {
    const urls = files.map((f) => URL.createObjectURL(f));
    // Previews follow the chosen files; the URLs are released on change.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPreviews(urls);
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [files]);

  const enrol = useMutation({
    mutationFn: async () => {
      const body = new FormData();
      files.forEach((file) => body.append("photos", file));
      await ensureOk(await rawFetch(`/v1/staff/${staff!.id}/face`, { method: "POST", body }));
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["staff"] });
      setFiles([]);
      onClose();
    },
  });

  return (
    <Modal
      open={!!staff}
      onClose={() => {
        setFiles([]);
        onClose();
      }}
      title={`${t("staff.enrolFace")} · ${staff?.full_name ?? ""}`}
    >
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          enrol.mutate();
        }}
      >
        <p className="text-sm text-slate-600">{t("staff.enrolHelp")}</p>
        <Field label={t("staff.photos")}>
          {(id) => (
            <Input
              id={id}
              type="file"
              accept="image/jpeg,image/png"
              multiple
              onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, MAX_PHOTOS))}
            />
          )}
        </Field>
        {previews.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {previews.map((url) => (
              // eslint-disable-next-line @next/next/no-img-element -- local preview, never uploaded elsewhere
              <img key={url} src={url} alt="" className="size-20 rounded object-cover" />
            ))}
          </div>
        )}
        <ErrorText error={enrol.error} />
        <Button type="submit" variant="primary" disabled={!files.length || enrol.isPending}>
          {enrol.isPending ? "…" : t("staff.enrolFace")}
        </Button>
      </form>
    </Modal>
  );
}

function StaffList({ branchId }: { branchId: string }) {
  const { t, locale } = useI18n();
  const { timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const staff = useStaff(branchId);
  const shifts = useShifts(branchId);
  const [form, setForm] = useState<StaffForm | null>(null);
  const [consentFor, setConsentFor] = useState<Staff | null>(null);
  const [enrolFor, setEnrolFor] = useState<Staff | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["staff"] });

  const save = useMutation({
    mutationFn: (f: StaffForm) => {
      const body = { employee_code: f.employee_code, full_name: f.full_name, shift_id: f.shift_id || null };
      return f.id
        ? unwrap(api.PATCH("/v1/staff/{staff_id}", { params: { path: { staff_id: f.id } }, body }))
        : unwrap(api.POST("/v1/staff", { body: { ...body, branch_id: f.branch_id } }));
    },
    onSuccess: () => {
      setForm(null);
      void refresh();
    },
  });
  const action = useMutation({
    mutationFn: async ({ staffId, kind }: { staffId: string; kind: "withdraw" | "deleteFace" | "left" }) => {
      const path = { params: { path: { staff_id: staffId } } };
      if (kind === "withdraw") return unwrap(api.DELETE("/v1/staff/{staff_id}/consent", path));
      if (kind === "deleteFace") return unwrap(api.DELETE("/v1/staff/{staff_id}/face", path));
      return unwrap(api.PATCH("/v1/staff/{staff_id}", { ...path, body: { status: "left" } }));
    },
    onSuccess: () => void refresh(),
  });

  const shiftName = (id: string | null) => shifts.data?.find((s) => s.id === id)?.name ?? t("staff.noShift");
  const dateFormat = new Intl.DateTimeFormat(intlLocale(locale), { dateStyle: "medium", timeZone: timezoneOf(branchId) });

  return (
    <>
      <div className="mb-2 flex justify-end">
        <Button variant="primary" onClick={() => setForm({ branch_id: branchId, employee_code: "", full_name: "", shift_id: "" })}>
          + {t("staff.add")}
        </Button>
      </div>
      <Card>
        {staff.isLoading ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("staff.fullName")}</th>
                <th>{t("staff.shift")}</th>
                <th>{t("staff.consent")}</th>
                <th>{t("staff.face")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(staff.data ?? []).map((s) => {
                const left = s.status === "left";
                return (
                  <tr key={s.id} className={left ? "opacity-50" : ""}>
                    <td>
                      <p className="font-medium">{s.full_name}</p>
                      <p className="text-xs text-slate-500">
                        {s.employee_code}
                        {left && ` · ${t("staff.left")}`}
                      </p>
                    </td>
                    <td>{shiftName(s.shift_id)}</td>
                    <td>
                      {s.consent_signed_at ? (
                        <Badge tone="green">{t("staff.consentSigned", { date: dateFormat.format(new Date(s.consent_signed_at)) })}</Badge>
                      ) : (
                        <Badge>{t("staff.noConsent")}</Badge>
                      )}
                    </td>
                    <td>
                      <Badge tone={s.face_enrolled ? "green" : "slate"}>
                        {s.face_enrolled ? t("staff.faceEnrolled") : t("staff.faceNotEnrolled")}
                      </Badge>
                    </td>
                    <td className="text-end whitespace-nowrap">
                      {!left && (
                        <>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() =>
                              setForm({ id: s.id, branch_id: s.branch_id, employee_code: s.employee_code, full_name: s.full_name, shift_id: s.shift_id ?? "" })
                            }
                          >
                            {t("common.edit")}
                          </Button>
                          {s.consent_signed_at ? (
                            <>
                              <Button size="sm" variant="ghost" onClick={() => setEnrolFor(s)}>
                                {t("staff.enrolFace")}
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => window.confirm(t("staff.withdrawConfirm")) && action.mutate({ staffId: s.id, kind: "withdraw" })}
                              >
                                {t("staff.withdrawConsent")}
                              </Button>
                            </>
                          ) : (
                            <Button size="sm" variant="ghost" onClick={() => setConsentFor(s)}>
                              {t("staff.recordConsent")}
                            </Button>
                          )}
                          {s.face_enrolled && (
                            <Button size="sm" variant="ghost" onClick={() => action.mutate({ staffId: s.id, kind: "deleteFace" })}>
                              {t("staff.deleteFace")}
                            </Button>
                          )}
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => window.confirm(t("staff.markLeftConfirm")) && action.mutate({ staffId: s.id, kind: "left" })}
                          >
                            {t("staff.markLeft")}
                          </Button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
        <ErrorText error={staff.error ?? action.error} />
      </Card>

      <Modal open={!!form} onClose={() => setForm(null)} title={form?.id ? t("common.edit") : t("staff.add")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate(form);
            }}
          >
            <Field label={t("staff.fullName")}>
              {(id) => <Input id={id} required maxLength={200} value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />}
            </Field>
            <Field label={t("staff.code")}>
              {(id) => <Input id={id} required maxLength={50} value={form.employee_code} onChange={(e) => setForm({ ...form, employee_code: e.target.value })} />}
            </Field>
            <Field label={t("staff.shift")}>
              {(id) => (
                <Select id={id} value={form.shift_id} onChange={(e) => setForm({ ...form, shift_id: e.target.value })}>
                  <option value="">{t("staff.noShift")}</option>
                  {(shifts.data ?? []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <ErrorText error={save.error} />
            <Button type="submit" variant="primary" disabled={save.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>
      <ConsentDialog key={consentFor?.id ?? "none"} staff={consentFor} onClose={() => setConsentFor(null)} />
      <EnrolDialog staff={enrolFor} onClose={() => setEnrolFor(null)} />
    </>
  );
}

function Shifts({ branchId }: { branchId: string }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const shifts = useShifts(branchId);
  const [form, setForm] = useState<ShiftForm | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["shifts"] });

  const save = useMutation({
    mutationFn: (f: ShiftForm) => {
      const body = { name: f.name, starts_at: f.starts_at, ends_at: f.ends_at, grace_minutes: f.grace_minutes };
      return f.id
        ? unwrap(api.PATCH("/v1/shifts/{shift_id}", { params: { path: { shift_id: f.id } }, body }))
        : unwrap(api.POST("/v1/shifts", { body: { ...body, branch_id: f.branch_id } }));
    },
    onSuccess: () => {
      setForm(null);
      void refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/shifts/{shift_id}", { params: { path: { shift_id: id } } })),
    onSuccess: () => void refresh(),
  });

  return (
    <>
      <div className="mb-2 flex justify-end">
        <Button onClick={() => setForm({ branch_id: branchId, name: "", starts_at: "08:00", ends_at: "16:00", grace_minutes: 10 })}>
          + {t("staff.addShift")}
        </Button>
      </div>
      <Card>
        <Table>
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("staff.startsAt")}</th>
              <th>{t("staff.endsAt")}</th>
              <th>{t("staff.grace")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(shifts.data ?? []).map((s) => (
              <tr key={s.id}>
                <td className="font-medium">{s.name}</td>
                <td dir="ltr" className="text-start">
                  {s.starts_at.slice(0, 5)}
                </td>
                <td dir="ltr" className="text-start">
                  {s.ends_at.slice(0, 5)}
                </td>
                <td>{s.grace_minutes}</td>
                <td className="text-end whitespace-nowrap">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      setForm({ id: s.id, branch_id: s.branch_id, name: s.name, starts_at: s.starts_at.slice(0, 5), ends_at: s.ends_at.slice(0, 5), grace_minutes: s.grace_minutes ?? 10 })
                    }
                  >
                    {t("common.edit")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => window.confirm(t("common.confirmDelete", { name: s.name })) && remove.mutate(s.id)}>
                    {t("common.delete")}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
        <ErrorText error={shifts.error ?? remove.error} />
      </Card>
      <Modal open={!!form} onClose={() => setForm(null)} title={form?.id ? t("common.edit") : t("staff.addShift")}>
        {form && (
          <form
            className="grid grid-cols-2 gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate(form);
            }}
          >
            <Field label={t("common.name")} className="col-span-2">
              {(id) => <Input id={id} required maxLength={100} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
            </Field>
            <Field label={t("staff.startsAt")}>
              {(id) => <Input id={id} type="time" required value={form.starts_at} onChange={(e) => setForm({ ...form, starts_at: e.target.value })} />}
            </Field>
            <Field label={t("staff.endsAt")}>
              {(id) => <Input id={id} type="time" required value={form.ends_at} onChange={(e) => setForm({ ...form, ends_at: e.target.value })} />}
            </Field>
            <Field label={t("staff.grace")} className="col-span-2">
              {(id) => (
                <Input id={id} type="number" min={0} max={240} value={form.grace_minutes} onChange={(e) => setForm({ ...form, grace_minutes: Number(e.target.value) })} />
              )}
            </Field>
            <div className="col-span-2">
              <ErrorText error={save.error} />
            </div>
            <Button type="submit" variant="primary" className="col-span-2" disabled={save.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>
    </>
  );
}

function StaffPageContent() {
  const { t } = useI18n();
  const { branchId } = useBranches();
  const [tab, setTab] = useState<"staff" | "shifts">("staff");
  if (!branchId) return <Notice>{t("staff.chooseBranch")}</Notice>;
  return (
    <>
      <div className="mb-3 flex gap-1 border-b border-slate-200">
        {(["staff", "shifts"] as const).map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => setTab(name)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm ${tab === name ? "border-brand-600 font-medium text-brand-700" : "border-transparent text-slate-600"}`}
          >
            {name === "staff" ? t("staff.title") : t("staff.shifts")}
          </button>
        ))}
      </div>
      {tab === "staff" ? <StaffList key={branchId} branchId={branchId} /> : <Shifts key={branchId} branchId={branchId} />}
    </>
  );
}

export default function StaffPage() {
  const { t } = useI18n();
  return (
    <RequirePermission anyOf={["staff_manage"]}>
      <PageHeader title={t("staff.title")} />
      <StaffPageContent />
    </RequirePermission>
  );
}
