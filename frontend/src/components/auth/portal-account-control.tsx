"use client"

import { ChevronDown, LoaderCircle, LogOut, UserRound } from "lucide-react"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { usePortalAccount } from "./portal-account-provider"
import styles from "./portal-login.module.css"

export function PortalAccountControl({ compact = false, side = "bottom" }: { compact?: boolean; side?: "top" | "bottom" }) {
  const account = usePortalAccount()
  if (!account?.session) return <button type="button" className={styles.loginTrigger} onClick={(event) => account?.openLogin(event.currentTarget)}>登录</button>
  return <div className={styles.account} data-side={side}>
    <DropdownMenu><DropdownMenuTrigger className={styles.accountTrigger} aria-label="账户菜单" title={account.session.user.username}><span className={styles.avatar}><UserRound size={17} /></span>{!compact && <><span className={styles.username}>{account.session.user.username}</span><ChevronDown size={14} /></>}</DropdownMenuTrigger>
      <DropdownMenuContent side={side} align="end" className={styles.accountMenu}><DropdownMenuItem disabled={account.pending} onSelect={(event) => { event.preventDefault(); void account.signOut() }}>{account.pending ? <LoaderCircle size={16} className={styles.spinner} /> : <LogOut size={16} />}退出登录</DropdownMenuItem></DropdownMenuContent>
    </DropdownMenu>
    {account.error && <p className={styles.accountError} role="alert">{account.error}</p>}
  </div>
}
