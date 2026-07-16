'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import PermissionGuard from '@/components/PermissionGuard';

export default function ImportJobsRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace('/admin/jobs?tab=imports');
  }, [router]);
  return <PermissionGuard module="tasks">{null}</PermissionGuard>;
}
