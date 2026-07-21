#########################################################################
#  OpenKore - Packet sending
#  This module contains functions for sending packets to the server.
#
#  This software is open source, licensed under the GNU General Public
#  License, version 2.
#  Basically, this means that you're allowed to modify and distribute
#  this software. However, if you distribute modified versions, you MUST
#  also distribute the source code.
#  See http://www.gnu.org/licenses/gpl.html for the full license.
########################################################################
#  by alisonrag
package Network::Send::kRO::RagexeRE_2021_11_03;

use strict;
use base qw(Network::Send::kRO::RagexeRE_2020_07_23);
use Log qw(debug);
use Utils qw(getTickCount getCoordString);
use Globals qw($char);

sub new {
	my ($class) = @_;
	my $self = $class->SUPER::new(@_);

	my %packets = (
		'0436' => ['map_login', 'a4 a4 a4 V2 C', [qw(accountID charID sessionID unknown tick sex)]],#23
		'00F3' => ['public_chat', 'v Z*', [qw(len message)]],
		# 0x035F was reassigned to ReqClickBuyingStore for PACKETVER >= 20130320
		# The actual WalkToXY packet for our PACKETVER 20211103 is 0x0881
		'0881' => ['character_move', 'a3', [qw(coords)]],
	);

	$self->{packet_list}{$_} = $packets{$_} for keys %packets;
	delete $self->{packet_list}{'0072'};  # remove old map_login so 0436 is the only one
	$self->{packet_lut}{'map_login'}      = '0436';
	$self->{packet_lut}{'public_chat'}    = '00F3';
	$self->{packet_lut}{'character_move'} = '0881';

	return $self;
}

# 0x0881,5 — WalkToXY for PACKETVER 20211103
# Override needed because ancestor RagexeRE_2011_12_20b::sendMove uses field
# name 'coordString' which doesn't match our 'coords' definition. Pass x/y so
# reconstruct_character_move in Network::Send can build coords correctly.
sub sendMove {
	my ($self, $x, $y) = @_;
	my $coords = getCoordString(int($x), int($y), 1);
	my $msg = pack('v a3', 0x0881, $coords);
	$self->sendToServer($msg);
	debug "Sent move to: $x, $y\n", "sendPacket", 2;
}

# 0x0436,23
sub sendMapLogin {
	my ($self, $accountID, $charID, $sessionID, $sex) = @_;
	my $msg;
	$sex = 0 if ($sex > 1 || $sex < 0); # Sex can only be 0 (female) or 1 (male)

	$msg = $self->reconstruct({
		switch => 'map_login',
		accountID => $accountID,
		charID => $charID,
		sessionID => $sessionID,
		unknown => 0,# 00 00 00 00
		tick => getTickCount,
		sex => $sex,
	});

	$self->sendToServer($msg);
	debug "Sent sendMapLogin\n", "sendPacket", 2;
}

# 0x00F3 public chat — "Name : message" (no null for packetver >= 20151001)
sub sendChat {
	my ($self, $message) = @_;
	my $name = ($char && $char->{name}) ? $char->{name} : 'Brokkr';
	my $data = "$name : $message";
	my $len  = 4 + length($data);
	my $msg  = pack("v v a*", 0x00F3, $len, $data);
	$self->sendToServer($msg);
	debug "Sent sendChat\n", "sendPacket", 2;
}

1;
