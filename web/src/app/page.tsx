"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { homePath } from "@/components/AppShell";
import { Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";

export default function Home() {
  const { status, me } = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (status === "anonymous") router.replace("/login/");
    if (status === "signed-in") router.replace(homePath(me));
  }, [status, me, router]);
  return <Spinner />;
}
