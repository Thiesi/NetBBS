; SPDX-License-Identifier: BSD-2-Clause
; NetBBS DOS communications fixture. Build: nasm -f bin -o SERIAL.COM door_serial.asm
; Runs through COM1 UART, preserves CP437/ANSI and waits for a caller byte.
; Compile with -DFOSSIL=1 to exercise an independently installed FOSSIL driver.
bits 16
org 100h
start:
%ifdef STATE
    ; Deliberately multi-node-safe fixture: each node owns its own score file.
    mov al, [82h]               ; first argument, node 1 or 2
    mov [filename+4], al
    mov dx, filename
    mov ax, 3d02h
    int 21h
    jnc .opened
    xor cx, cx
    mov ah, 3ch
    int 21h
    jc failed
.opened:
    mov bx, ax
    xor cx, cx
    xor dx, dx
    mov ax, 4202h               ; seek end
    int 21h
    jc failed
    mov dx, score
    mov cx, 1
    mov ah, 40h
    int 21h
    jc failed
    mov ah, 3eh
    int 21h
%endif
%ifdef FOSSIL
    mov dx, 0
    mov ax, 0400h
    int 14h
    cmp ax, 1954h
    jne failed
%else
    mov dx, 3fbh
    mov al, 80h
    out dx, al
    mov dx, 3f8h
    mov al, 3                    ; 38400 baud
    out dx, al
    inc dx
    xor al, al
    out dx, al
    mov dx, 3fbh
    mov al, 3                    ; 8N1
    out dx, al
    mov dx, 3fch
    mov al, 3                    ; DTR + RTS
    out dx, al
%endif
    mov si, greeting
.send:
    lodsb
    test al, al
    jz .receive
    call putchar
    jmp .send
.receive:
%ifdef FOSSIL
    mov dx, 0
    mov ah, 2
    int 14h
%else
    mov dx, 3fdh
    in al, dx
    test al, 1
    jz .receive
    mov dx, 3f8h
    in al, dx
%endif
    call putchar
    cmp al, 'Q'
    jne .receive
    mov ax, 4c00h
    int 21h
failed:
    mov ax, 4c01h
    int 21h
putchar:
    push ax
%ifdef FOSSIL
    mov dx, 0
    mov ah, 1
    int 14h
%else
    push ax
.wait:
    mov dx, 3fdh
    in al, dx
    test al, 20h
    jz .wait
    pop ax
    mov dx, 3f8h
    out dx, al
%endif
    pop ax
    ret
greeting: db 27, '[32mDOS READY ', 0dbh, 27, '[0m', 13, 10, 0
%ifdef STATE
filename: db 'NODE0.DAT', 0
score: db 'X'
%endif
