<?php
/**
 * (c) 2004-2007 Linbox / Free&ALter Soft, http://linbox.com
 * (c) 2007-2008 Mandriva, http://www.mandriva.com/
 *
 * $Id$
 *
 * This file is part of Mandriva Management Console (MMC).
 *
 * MMC is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * MMC is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with MMC; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
 */
?>
<?

include ("user-xmlrpc.inc.php");

function _samba_infoUser($userObjClass) {
    // FIXME: test should be case insensitive
    if (array_search("sambaSamAccount",$userObjClass) || array_search("sambaSAMAccount",$userObjClass))
        print "sambaAccount ";
}

function _samba_delUser($uid) {
    if (hasSmbAttr($uid)) rmSmbAttr($uid);
}

function _samba_enableUser($uid) {
    if (hasSmbAttr($uid)) xmlCall("samba.enableUser", array($uid));
}

function _samba_disableUser($uid) {
    if (hasSmbAttr($uid)) xmlCall("samba.disableUser", array($uid));
}

function _samba_changeUserPasswd($paramsArr) {
    if (hasSmbAttr($paramsArr[0])) return xmlCall("samba.changeUserPasswd", $paramsArr);
}

function _samba_changeUserPrimaryGroup($uid, $group) {
    xmlCall("samba.changeUserPrimaryGroup",array($uid, $group));
}
 

function _samba_changeUser($postArr) {
    global $error;

    if ($error) return -1;

    if  (hasSmbAttr($postArr["nlogin"])) { //if it is an smbUser
        if (!$postArr["isSamba"]) {
            // Removing all SAMBA attributes
            rmSmbAttr($postArr["nlogin"]);
            global $result;
            $result .= _T("Samba attributes deleted.","samba")."<br />";
        } else {
            // Change SAMBA attributes
            /*
             * Remove passwords from the $_POST array coming from the add/edit user page
             * because we don't want to send them again via RPC.
             */
            unset($postArr["pass"]);
            unset($postArr["confpass"]);

            changeSmbAttr($postArr["nlogin"], $postArr);
            if (isEnabledUser($postArr["nlogin"])) {
                if ($_POST['isSmbDesactive']) {
                    smbDisableUser($postArr["nlogin"]);
                }
            } else {
                if (!$_POST['isSmbDesactive']) {
                    smbEnableUser($postArr["nlogin"]);
                }
            }

            if (isLockedUser($postArr["nlogin"])) {
                if (!$_POST['isSmbLocked']) {
                    smbUnlockUser($postArr["nlogin"]);
                }
            } else {
                if ($_POST['isSmbLocked']) {
                    smbLockUser($postArr["nlogin"]);
                }
            }
        }
    }
    else { //if not have smb attributes
        if ($postArr["isSamba"]) {
            global $result;
            $result.=_T("Samba attributes added.","samba")."<br />";
            addSmbAttr($postArr["nlogin"],$postArr["pass"]);
            changeSmbAttr($postArr["nlogin"], $postArr);
        }
    }
}

function _samba_verifInfo($postArr) {

    if ($postArr["isSamba"]) {
        if (!hasSmbAttr($postArr["nlogin"])) {
            if ($postArr["pass"]=="") { //if we not precise password
                global $error;
                $error.= _T("You must reenter your password.","samba")."<br />\n";
                setFormError("pass");
                return -1;
            }
        }
        $drive = $postArr["sambaHomeDrive"];
        if ($drive != "") {

            // Check that the SAMBA home drive is a correct drive letter
            // "X:", "U:", etc.
            $err = False;
            //if (strlen($drive) != 2) $err = True;
            //elseif ($drive{1} != ":") $err = True;
            if (!preg_match("/^[C-Z]:$/", $drive)) {
                global $error;
                $error .= _T("Invalid network drive.","samba")."<br />\n";
                setFormError("sambaHomeDrive");
                return -1;
            }
        }
    }
}

function _samba_baseEdit($ldapArr,$postArr) {
    $checked = "checked"; //default value
    $displayType= "inline";

    //fetch ldap updated info if we can
    if (isset($ldapArr["uid"][0])) {
        $uid = $ldapArr["uid"][0];
        if (!hasSmbAttr($ldapArr["uid"][0])) {
            $checked = "";
            $displayType= "none";
        }
    }

    //if we update a user but error when updating
    if ($postArr["buser"]) {
        if ($postArr["isSamba"]) {
            $checked = "checked";
            $displayType= "inline";
        } else {
            $checked = "";
            $displayType= "none";
        }
    }
    print "<div class=\"formblock\" style=\"background-color: #EFE;\">";
    print "<h3>"._T("Samba user properties","samba")."</h3>\n";
    print '<table cellspacing="0">';

    $test = new TrFormElement(_T("SAMBA access","samba"), new CheckboxTpl("isSamba"));
    $test->setCssError("accesSmb");
    $param = array("value"=>$checked,
                   "extraArg"=>'onclick="toggleVisibility(\'smbtable\');"');
    $test->display($param);


    print '</table>'."\n";


    print '<div id="smbtable" style="display: '.$displayType.';">'."\n";


    print '<table>'."\n";

    $checked = "";
    $smbuser = False;
    if (isset($ldapArr["uid"][0])) {
        $smbuser = hasSmbAttr($ldapArr["uid"][0]);
        if ($smbuser) {
            if (!isEnabledUser($ldapArr["uid"][0])) {
                $checked = "checked";
            }
        }
    }

    if ($smbuser) {
        if (userPasswdHasExpired($ldapArr["uid"][0])) {
            $formElt = new HiddenTpl("userPasswdHasExpired");
            $warn = new TrFormElement(_("WARNING"), $formElt);
            $warn->display(array("value" => _T("The user password has expired", "samba")));
        }
    }

    $param = array ("value" => $checked);

    $test = new TrFormElement(_T("User is disabled, if checked","samba"), new CheckboxTpl("isSmbDesactive"),
                                array("tooltip"=>
                                _T("Disable samba user account",'samba')));
    $test->setCssError("isSmbDesactive");
    $test->display($param);

    if ($smbuser) {
        if (isLockedUser($ldapArr["uid"][0])) {
            $checked = "checked";
        } else {
            $checked = "";
        }
    }
    $param = array ("value" => $checked);

    $tr = new TrFormElement(_T("User is locked, if checked","samba"), new CheckboxTpl("isSmbLocked"),
                        array("tooltip"=>
                        _T("Lock samba user access
                        <p>User can be locked after too many failed log.</p>",'samba')));
    $tr->setCssError("isSmbLocked");
    $tr->display($param);

    print '</table>'."\n";


    // Expert mode display
    print '<div id="expertMode" ';
    displayExpertCss();
    print '><table>';

    $d = array(_T("User profile path","samba") => "sambaProfilePath",
               _T("Opening script session","samba") => "sambaLogonScript",
               _T("Base directory path","samba") => "sambaHomePath",
               _T("Connect base directory on network drive","samba") => "sambaHomeDrive")    ;


    foreach ($d as $description => $field) {
        $tr = new TrFormElement($description,
                                  new InputTpl($field));
        $tr->setCssError($field);
        if (isset($ldapArr[$field][0])) {
            $value = $ldapArr[$field][0];
        } else {
            $value = "";
        }
        $tr->display(array("value"=>$value));
    }

    print '</table>'."\n";
    print "</div>"."\n";
    print "</div>"."\n";
    print "</div>"."\n";

}





?>
